from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent import retrieval  # noqa: E402


def test_search_raw_inputs_delegates_to_existing_pgvector_client(monkeypatch):
    expected = [SimpleNamespace(id="hit-1")]
    captured = {}

    def fake_search_similar(session, **kwargs):
        captured["session"] = session
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(retrieval.raw_inputs, "search_similar", fake_search_similar)
    session = SimpleNamespace()
    exclude_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    hits = retrieval.search_raw_inputs(
        session,
        embedding=[0.1, 0.2],
        query="rent invoice",
        exclude_id=exclude_id,
        statuses=["open"],
        k=4,
    )

    assert hits is expected
    assert captured == {
        "session": session,
        "embedding": [0.1, 0.2],
        "query": "rent invoice",
        "exclude_id": exclude_id,
        "statuses": ["open"],
        "k": 4,
    }


def test_precedent_query_text_prefers_subject_then_content():
    from types import SimpleNamespace

    assert (
        retrieval.precedent_query_text(
            SimpleNamespace(source_metadata={"subject": "Rent invoice"}, content="body")
        )
        == "Rent invoice"
    )
    assert (
        retrieval.precedent_query_text(SimpleNamespace(source_metadata={}, content="buy milk"))
        == "buy milk"
    )


@pytest.mark.asyncio
async def test_search_notes_uses_gemini_embedding_and_notes_store(monkeypatch):
    captured = {}

    async def fake_embed(query):
        captured["query"] = query
        return [0.3, 0.4]

    def fake_search_similar(session, **kwargs):
        captured["session"] = session
        captured.update(kwargs)
        return [
            SimpleNamespace(
                similarity=0.876,
                content="Alice owns Project Alpha",
                created_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
                source_from="alice@example.com",
                source_subject="Project Alpha kickoff",
            )
        ]

    monkeypatch.setattr(retrieval, "embed", fake_embed)
    monkeypatch.setattr(retrieval.notes_store, "search_similar", fake_search_similar)
    session = SimpleNamespace()

    text = await retrieval.search_notes(session, query=" Project Alpha ", k=5)

    assert captured == {
        "query": "Project Alpha",
        "session": session,
        "embedding": [0.3, 0.4],
        "k": 5,
    }
    assert text == (
        "Notes:\n- sim=0.88 · 2026-05-12 · from: alice@example.com · "
        "subject: Project Alpha kickoff | Alice owns Project Alpha"
    )


@pytest.mark.asyncio
async def test_search_notes_reports_disabled_embeddings(monkeypatch):
    async def fake_embed(query):
        return None

    monkeypatch.setattr(retrieval, "embed", fake_embed)

    text = await retrieval.search_notes(SimpleNamespace(), query="anything")

    assert text == "search_notes: embeddings disabled — no notes available."
