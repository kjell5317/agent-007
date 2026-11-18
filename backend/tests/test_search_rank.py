from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.db.schemas.search import SearchHit  # noqa: E402
from app.services.search import rank as R  # noqa: E402
from app.services.search import search as search_mod  # noqa: E402


def test_lexical_score_tolerates_typos_and_partial_words():
    assert R.lexical_score("invoice", "Pay the invoice") == 1.0
    assert R.lexical_score("presentat", "Prepare the presentation") == 1.0
    assert R.lexical_score("invoce", "Pay the invoice") > 0.4
    assert R.lexical_score("invoice", "Book a flight") < 0.3
    assert R.lexical_score("", "anything") == 0.0
    # Umlauts and other letters count as word characters.
    assert R.lexical_score("prüfung", "Prüfung Analysis") == 1.0


def test_rank_fuses_semantic_and_lexical_by_rank():
    exact = R.Candidate("exact", "Pay the invoice", semantic=0.70)
    meaning = R.Candidate("meaning", "Settle the bill", semantic=0.90)
    noise = R.Candidate("noise", "Book a flight", semantic=0.60)
    ranked = [c.hit for c in R.rank("pay invoice", [noise, meaning, exact])]
    # Matching both ways beats matching on meaning alone; noise comes last.
    assert ranked == ["exact", "meaning", "noise"]


def test_rank_keeps_incoming_order_on_ties():
    a, b = R.Candidate("a", "x"), R.Candidate("b", "y")
    assert [c.hit for c in R.rank("zzz", [a, b])] == ["a", "b"]


def _hit(type_, id_, title, snippet=None, source=None) -> SearchHit:
    return SearchHit(type=type_, id=id_, title=title, snippet=snippet, source=source, score=0.0)


@pytest.mark.asyncio
async def test_run_search_sorts_every_source_together(monkeypatch):
    task = _hit("task", "t1", "Pay the invoice")
    email = _hit("input", "i1", "Your invoice is ready", source="gmail")
    event = _hit("document", "e1", "Team lunch", source="calendar")
    github = _hit("github", "acme/app#7", "Invoice export is broken")
    drive = _hit("drive", "f1", "Holiday photos")

    async def embed(_text):
        return [1.0, 0.0]

    async def embed_many(texts):
        vectors = {"Pay the invoice": [0.9, 0.1], "Invoice export is broken": [0.8, 0.2],
                   "Holiday photos": [0.0, 1.0]}
        return [vectors.get(t.split("\n")[0]) for t in texts]

    async def live(provider, _text, _format):
        return {"github": [github], "drive": [drive], "contact": []}[provider]

    monkeypatch.setattr(search_mod, "embed", embed)
    monkeypatch.setattr(search_mod, "embed_many", embed_many)
    monkeypatch.setattr(search_mod, "_live", live)
    monkeypatch.setattr(search_mod, "_local", lambda *_a: [task, email])
    monkeypatch.setattr(search_mod, "run_suggest", lambda *_a, **_k: [event])
    monkeypatch.setattr(search_mod.search_client, "stored_similarities",
                        lambda _s, _e, refs: {("input", "i1"): 0.85, ("document", "e1"): 0.1})

    hits = await search_mod.run_search(SimpleNamespace(), "invoice")

    # All three match "invoice" word for word, so meaning orders them.
    assert [h.id for h in hits][:3] == ["t1", "acme/app#7", "i1"]
    assert {h.id for h in hits[3:]} == {"f1", "e1"}


@pytest.mark.asyncio
async def test_run_search_ranks_by_words_when_embeddings_are_unavailable(monkeypatch):
    async def no_embedding(_text):
        raise RuntimeError("embedding provider down")

    async def nothing(*_a):
        return []

    monkeypatch.setattr(search_mod, "embed", no_embedding)
    monkeypatch.setattr(search_mod, "_live", nothing)
    monkeypatch.setattr(search_mod, "_local", lambda *_a: [
        _hit("task", "t2", "Book a flight"), _hit("task", "t1", "Pay the invoice"),
    ])
    hits = await search_mod.run_search(SimpleNamespace(), "invoice", kind="tasks")
    assert [h.id for h in hits] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_filter_kind_picks_the_sources(monkeypatch):
    asked = []

    async def live(provider, _text, _format):
        asked.append(provider)
        return []

    async def embed(_text):
        return None

    monkeypatch.setattr(search_mod, "embed", embed)
    monkeypatch.setattr(search_mod, "_live", live)
    monkeypatch.setattr(search_mod, "_local", lambda *_a: asked.append("local") or [])
    monkeypatch.setattr(search_mod, "run_suggest", lambda *_a, **_k: asked.append("events") or [])

    await search_mod.run_search(SimpleNamespace(), "x", kind="contacts")
    assert asked == ["contact"]
    asked.clear()
    await search_mod.run_search(SimpleNamespace(), "x", kind="files")
    assert sorted(asked) == ["drive", "local"]
    asked.clear()
    await search_mod.run_search(SimpleNamespace(), "x")
    assert sorted(asked) == ["contact", "drive", "events", "github", "local"]
    asked.clear()
    await search_mod.run_search(SimpleNamespace(), "x", kind="events")
    assert sorted(asked) == ["calendar", "events"]
    asked.clear()
    await search_mod.run_search(SimpleNamespace(), "x", kind="github")
    assert asked == ["github"]


def _dated(type_, id_, title, ts=None, source=None, meta=None, url=None) -> SearchHit:
    return SearchHit(type=type_, id=id_, title=title, source=source, ts=ts, meta=meta, url=url, score=0.0)


def test_time_distance_counts_the_future_like_the_past_for_every_source():
    now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
    week = timedelta(days=7)
    for when in (now - week, now + week):
        hits = [
            _dated("task", "t", "Task", ts=now, meta={"due_date": when.isoformat()}),
            _dated("document", "e", "Event", ts=when, source="calendar"),
            _dated("input", "i", "Mail", ts=when, source="gmail"),
            _dated("note", "n", "Note", ts=when),
            _dated("drive", "f", "File", ts=when),
            _dated("document", "k", "Brief", ts=when, source="kotx"),
            _dated("github", "acme/app#1", "Issue", ts=when),
        ]
        assert {R.time_distance(h, now, "x") for h in hits} == {week.total_seconds()}
    assert R.time_distance(_dated("drive", "f", "File"), now, "x") is None


def test_contacts_stand_in_match_quality_for_a_date():
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    exact = _dated("contact", "c1", "Alice Smith", meta={"emails": ["alice@example.com"]})
    prefix = _dated("contact", "c2", "Alice Johnson")
    weak = _dated("contact", "c3", "Bob", meta={"org": "Alice's Office"})
    assert R.time_distance(exact, now, "Alice Smith") == 0
    assert R.time_distance(exact, now, "alice@example.com") == 0
    assert R.time_distance(prefix, now, "Alice") == 86_400
    assert R.time_distance(weak, now, "Alice") == 5 * 86_400


def test_rank_puts_dates_near_now_first_either_way():
    far_past = R.Candidate("far_past", "Dentist", distance=400 * 86_400)
    near = R.Candidate("near", "Dentist", distance=86_400)
    far_future = R.Candidate("far_future", "Dentist", distance=400 * 86_400)
    undated = R.Candidate("undated", "Dentist")
    ranked = [c.hit for c in R.rank("dentist", [far_past, far_future, undated, near])]
    assert ranked == ["near", "far_past", "far_future", "undated"]


@pytest.mark.asyncio
async def test_run_search_orders_mixed_sources_by_distance_from_now(monkeypatch):
    now = datetime.now(timezone.utc)
    year, day = timedelta(days=365), timedelta(days=1)
    old_mail = _dated("input", "i1", "Dentist bill", ts=now - year, source="gmail")
    far_task = _dated("task", "t1", "Dentist checkup", ts=now, meta={"due_date": (now + year).isoformat()})
    issue = _dated("github", "acme/app#1", "Dentist booking bug", ts=now - day)
    event = _dated("document", "e1", "Dentist", ts=now + day, source="calendar")

    async def embed(_text):
        return None

    async def live(provider, _text, _format):
        return {"github": [issue], "drive": [], "contact": []}[provider]

    monkeypatch.setattr(search_mod, "embed", embed)
    monkeypatch.setattr(search_mod, "_live", live)
    monkeypatch.setattr(search_mod, "_local", lambda *_a: [old_mail, far_task])
    monkeypatch.setattr(search_mod, "run_suggest", lambda *_a, **_k: [event])

    hits = await search_mod.run_search(SimpleNamespace(), "dentist")

    assert {h.id for h in hits[:2]} == {"acme/app#1", "e1"}
    assert {h.id for h in hits[2:]} == {"i1", "t1"}


def test_a_cached_event_and_its_live_copy_show_once():
    link = "https://www.google.com/calendar/event?eid=abc"
    cached = _dated("document", "doc-uuid", "Lunch", source="calendar", url=link)
    live = _dated("document", "event-id", "Lunch", source="calendar", url=link)
    other = _dated("document", "event-2", "Dinner", source="calendar", url=link + "2")
    kept = search_mod.drop_duplicate_events([cached, live, other])
    assert [h.id for h in kept] == ["doc-uuid", "event-2"]


@pytest.mark.asyncio
async def test_closed_and_recurring_narrow_the_local_task_search(monkeypatch):
    captured = {}

    def hybrid(_session, **kwargs):
        captured.update(kwargs)
        return []

    async def embed(_text):
        return None

    monkeypatch.setattr(search_mod, "embed", embed)
    monkeypatch.setattr(search_mod.search_client, "hybrid_search", hybrid)
    monkeypatch.setattr(search_mod, "get_settings", lambda: SimpleNamespace(
        min_input_chars=0, notes_semantic_min_similarity=0.0,
    ))

    await search_mod.run_search(SimpleNamespace(), "gym", kind="tasks", status="closed", recurring=True)
    assert captured["corpora"] == frozenset({"task"})
    assert captured["status"] == "closed"
    assert captured["recurring"] is True
