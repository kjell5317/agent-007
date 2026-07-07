"""Retrieval helpers over the existing pgvector stores.

Thin wrappers around `app.db.clients` so the agent flows don't reach into the
storage layer directly. No change to Gemini embeddings or pgvector storage.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.db.clients import notes as notes_store, raw_inputs
from app.db.clients.raw_inputs import SimilarInput
from app.services.input.embedding import embed


def search_raw_inputs(
    session: Any,
    *,
    embedding: list[float],
    exclude_id: uuid.UUID,
    statuses: list[str],
    k: int,
) -> list[SimilarInput]:
    return raw_inputs.search_similar(
        session,
        embedding=embedding,
        exclude_id=exclude_id,
        statuses=statuses,
        k=k,
    )


async def search_notes(session: Any, *, query: str, k: int = 5) -> str:
    """Embed a note query with Gemini and search stored note embeddings."""
    query = (query or "").strip()
    if not query:
        return "search_notes: empty query."
    vec = await embed(query)
    if vec is None:
        return "search_notes: embeddings disabled — no notes available."
    hits = notes_store.search_similar(session, embedding=vec, query=query, k=k)
    if not hits:
        return "search_notes: no matching notes."
    return "Notes:\n" + "\n".join(_note_line(h) for h in hits)


def _note_line(h) -> str:
    parts = [f"sim={h.similarity:.2f}"]
    if h.created_at is not None:
        parts.append(h.created_at.date().isoformat())
    if h.source_from:
        parts.append(f"from: {h.source_from}")
    if h.source_subject:
        parts.append(f"subject: {h.source_subject}")
    return "- " + " · ".join(parts) + f" | {h.content}"
