"""Unified retrieve() routing (DB-free): source picks the backends and the
before/after filters reach the calendar + Drive API calls. Plus the rich-file
text extractor."""

from __future__ import annotations

import io
import zipfile

import pytest

import importlib

from app.db.clients.documents import CalendarMatch
from app.db.clients.search import SuggestHit
from app.services.search.extract import extract_text
from app.services.search.filters import Filters
from app.services.search.retrieve import retrieve

# The package re-exports the `retrieve` function, shadowing the submodule as an
# attribute — grab the module itself so monkeypatch targets its bindings.
retrieve_mod = importlib.import_module("app.services.search.retrieve")

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _suggest(type_: str, id_: str) -> SuggestHit:
    return SuggestHit(
        type=type_, id=id_, title="t", snippet=None, url=None, task_id=None,
        source=None, sender=None, status=None, ts=None, score=1.0,
    )


def _patch_backends(monkeypatch, calls):
    async def fake_embed(text, **kw):
        return [0.1, 0.2, 0.3, 0.4]

    def fake_hybrid(session, **kw):
        calls["local"] += 1
        return [_suggest("task", "t1")]

    def fake_cal(session, **kw):
        calls["cal"] += 1
        calls["cal_kw"] = kw
        return [
            CalendarMatch(
                event_id="e1", calendar_id=None, summary="Standup", location=None,
                starts_at=None, similarity=0.9, url="http://c",
            )
        ]

    async def fake_drive(session, query, *, k, timeout, after=None, before=None):
        calls["drive"] += 1
        calls["drive_after"], calls["drive_before"] = after, before
        return []

    monkeypatch.setattr(retrieve_mod, "embed", fake_embed)
    monkeypatch.setattr(retrieve_mod.search_client, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(retrieve_mod.documents_store, "search_calendar_semantic", fake_cal)
    monkeypatch.setattr(retrieve_mod, "search_drive", fake_drive)


@pytest.mark.asyncio
async def test_no_source_fans_out_to_all_backends(monkeypatch):
    calls = {"local": 0, "cal": 0, "drive": 0}
    _patch_backends(monkeypatch, calls)
    hits = await retrieve(object(), "standup")
    assert (calls["local"], calls["cal"], calls["drive"]) == (1, 1, 1)
    # Calendar hit carries the event id (so update_event can use it).
    assert any(h.type == "document" and h.id == "e1" for h in hits)


@pytest.mark.asyncio
async def test_source_calendar_runs_calendar_only_with_window(monkeypatch):
    calls = {"local": 0, "cal": 0, "drive": 0}
    _patch_backends(monkeypatch, calls)
    await retrieve(object(), "dentist", filters=Filters(source="calendar", after="2026-07-01"))
    assert (calls["local"], calls["cal"], calls["drive"]) == (0, 1, 0)
    # before/after reach the calendar API as the time window.
    assert calls["cal_kw"]["time_min"] == "2026-07-01"


@pytest.mark.asyncio
async def test_source_drive_runs_drive_only_with_window(monkeypatch):
    calls = {"local": 0, "cal": 0, "drive": 0}
    _patch_backends(monkeypatch, calls)
    await retrieve(object(), "budget", filters=Filters(source="drive", before="2026-08-01"))
    assert (calls["local"], calls["cal"], calls["drive"]) == (0, 0, 1)
    # before/after reach the Drive API call.
    assert calls["drive_before"] == "2026-08-01"


@pytest.mark.asyncio
async def test_specific_input_source_runs_local_only(monkeypatch):
    calls = {"local": 0, "cal": 0, "drive": 0}
    _patch_backends(monkeypatch, calls)
    await retrieve(object(), "invoice", filters=Filters(source="gmail"))
    assert (calls["local"], calls["cal"], calls["drive"]) == (1, 0, 0)


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
