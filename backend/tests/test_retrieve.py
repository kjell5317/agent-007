"""Per-source retrieval (DB-free): the up-front `retrieve` pre-injects tasks +
notes only, and each drill-down path (`search_messages`, `search_calendar`,
`search_tasks`) targets its own corpus/backend. Plus the rich-file extractor."""

from __future__ import annotations

import io
import zipfile

import pytest

import importlib
from datetime import datetime, timezone
from types import SimpleNamespace

from app.config import get_settings
from app.db.clients.documents import CalendarMatch
from app.db.clients.search import SuggestHit
from app.db.schemas.search import SearchHit
from app.services.search.extract import extract_text
from app.services.search.retrieve import (
    find_tasks,
    list_tasks,
    retrieve,
    search_messages,
    search_tasks,
)

# The package re-exports the functions, shadowing the submodule as an attribute —
# grab the module itself so monkeypatch targets its bindings.
retrieve_mod = importlib.import_module("app.services.search.retrieve")

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _suggest(
    type_: str, id_: str, *, status: str | None = None, similarity: float | None = None
) -> SuggestHit:
    return SuggestHit(
        type=type_, id=id_, title="t", snippet=None, url=None, task_id=None,
        source=None, sender=None, status=status, ts=None, score=1.0, similarity=similarity,
    )


def _patch_hybrid(monkeypatch, calls, rows=None):
    async def fake_embed(text, **kw):
        return [0.1, 0.2, 0.3, 0.4]

    def fake_hybrid(session, **kw):
        calls.append(kw)
        return rows if rows is not None else [_suggest("task", "t1")]

    monkeypatch.setattr(retrieve_mod, "embed", fake_embed)
    monkeypatch.setattr(retrieve_mod.search_client, "hybrid_search", fake_hybrid)


@pytest.mark.asyncio
async def test_retrieve_preinjects_tasks_and_notes_only(monkeypatch):
    calls: list[dict] = []
    _patch_hybrid(monkeypatch, calls)
    # A calendar/drive backend call here would be a regression — retrieve must
    # not fan out. Point them at raisers so any call blows up the test.
    monkeypatch.setattr(
        retrieve_mod.documents_store, "search_calendar_semantic",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("calendar called")),
    )
    hits = await retrieve(object(), "standup")
    assert len(calls) == 1
    assert calls[0]["corpora"] == frozenset({"task", "note"})
    # The note vector floor is threaded so far-off notes can't ride into context.
    assert calls[0]["note_min_similarity"] == get_settings().notes_semantic_min_similarity
    assert [h.id for h in hits] == ["t1"]


def test_searchhit_build_maps_similarity_to_meta():
    # The regression: pre-injection note hits carry a cosine, but SearchHit.build
    # dropped it, so the uniform record never showed `sim=`.
    assert SearchHit.build(_suggest("note", "n1", similarity=0.82)).meta == {"similarity": 0.82}
    # Keyword-only (no cosine) or a non-positive one shows no misleading number.
    assert SearchHit.build(_suggest("note", "n2")).meta is None
    assert SearchHit.build(_suggest("note", "n3", similarity=0.0)).meta is None


@pytest.mark.asyncio
async def test_search_messages_restricts_to_inputs_with_filters(monkeypatch):
    calls: list[dict] = []
    _patch_hybrid(monkeypatch, calls, rows=[_suggest("input", "i1")])
    monkeypatch.setattr(retrieve_mod, "_attach_input_source_urls", lambda s, h: None)
    hits = await search_messages(object(), "invoice", source="gmail", before="2026-08-01")
    assert calls[0]["corpora"] == frozenset({"input"})
    assert calls[0]["source"] == "gmail"
    assert calls[0]["before"] == "2026-08-01"
    assert [h.id for h in hits] == ["i1"]


@pytest.mark.asyncio
async def test_search_tasks_keyword_only_with_status_postfilter(monkeypatch):
    calls: list[dict] = []
    _patch_hybrid(
        monkeypatch, calls,
        rows=[_suggest("task", "t1", status="open"), _suggest("task", "t2", status="closed")],
    )
    hits = await search_tasks(object(), "report", label="uni", status="open")
    assert calls[0]["corpora"] == frozenset({"task"})
    assert calls[0]["label"] == "uni"
    # Tasks are keyword-only — no embedding is passed.
    assert calls[0]["embedding"] is None
    # `status` is derived, so it's a post-filter on the resolved hits.
    assert [h.id for h in hits] == ["t1"]


@pytest.mark.asyncio
async def test_search_calendar_query_mode_carries_event_id(monkeypatch):
    async def fake_embed(text, **kw):
        return [0.1, 0.2, 0.3, 0.4]

    seen = {}

    def fake_cal(session, **kw):
        seen.update(kw)
        return [
            CalendarMatch(
                event_id="e1", calendar_id=None, summary="Standup", location=None,
                starts_at=None, similarity=0.9, url="http://c",
            )
        ]

    monkeypatch.setattr(retrieve_mod, "embed", fake_embed)
    monkeypatch.setattr(retrieve_mod.documents_store, "search_calendar_semantic", fake_cal)
    hits = await retrieve_mod.search_calendar(object(), query="standup", time_max="2026-08-01")
    assert seen["time_max"] == "2026-08-01"
    assert [(h.type, h.id, h.source) for h in hits] == [("document", "e1", "calendar")]


@pytest.mark.asyncio
async def test_find_tasks_routes_query_to_keyword_and_empty_to_listing(monkeypatch):
    seen = {}

    async def fake_search(session, query, *, label=None, status=None):
        seen["mode"] = "search"
        seen["query"] = query
        return []

    def fake_list(session, *, status, due_after, due_before, label):
        seen["mode"] = "list"
        seen["status"] = status
        return []

    monkeypatch.setattr(retrieve_mod, "search_tasks", fake_search)
    monkeypatch.setattr(retrieve_mod, "list_tasks", fake_list)

    await find_tasks(object(), query="groceries")
    assert seen["mode"] == "search" and seen["query"] == "groceries"

    # No query → listing, defaulting to open.
    await find_tasks(object(), due_before="2026-07-09")
    assert seen["mode"] == "list" and seen["status"] == "open"


def _task(id_, title, *, due=None, scheduled=None, label=None):
    return SimpleNamespace(
        id=id_, title=title, description=None, link=None, label=label,
        due_date=due, scheduled_date=scheduled,
    )


def test_list_tasks_filters_by_due_window(monkeypatch):
    rows = [
        (_task("t1", "Today", due=datetime(2026, 7, 8, 12, tzinfo=timezone.utc)), "open"),
        (_task("t2", "Tomorrow", due=datetime(2026, 7, 9, 12, tzinfo=timezone.utc)), "open"),
        (_task("t3", "No date"), "open"),
    ]
    monkeypatch.setattr(
        retrieve_mod.tasks_store, "list_", lambda session, *, status, limit: rows
    )
    hits = list_tasks(object(), status="open", due_after="2026-07-08", due_before="2026-07-09")
    assert [h.id for h in hits] == ["t1"]
    assert hits[0].type == "task" and hits[0].task_id == "t1"


def test_list_tasks_no_window_returns_all_with_label_filter(monkeypatch):
    rows = [
        (_task("t1", "Uni thing", label="Uni"), "open"),
        (_task("t2", "Work thing", label="Work"), "open"),
    ]
    monkeypatch.setattr(
        retrieve_mod.tasks_store, "list_", lambda session, *, status, limit: rows
    )
    assert [h.id for h in list_tasks(object(), status="open")] == ["t1", "t2"]
    assert [h.id for h in list_tasks(object(), status="open", label="uni")] == ["t1"]


def test_extract_text_from_ooxml_docx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            "<?xml version='1.0'?>"
            "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
            "<w:body><w:p><w:r><w:t>Hello</w:t></w:r>"
            "<w:r><w:t>world</w:t></w:r></w:p></w:body></w:document>",
        )
    out = extract_text(_DOCX, buf.getvalue(), max_chars=1000)
    assert out == "Hello world"


def test_extract_text_unsupported_returns_none():
    assert extract_text("image/png", b"\x89PNG", max_chars=100) is None
