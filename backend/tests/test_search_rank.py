from __future__ import annotations

import os
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

    assert [h.id for h in hits][:3] == ["t1", "i1", "acme/app#7"]
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
