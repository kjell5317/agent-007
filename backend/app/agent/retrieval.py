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
    hits = notes_store.search_similar(session, embedding=vec, k=k)
    if not hits:
        return "search_notes: no matching notes."
    lines = [f"- sim={h.similarity:.2f} | {h.content}" for h in hits]
    return "Notes:\n" + "\n".join(lines)
