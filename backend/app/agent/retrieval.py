"""Haystack-friendly retrieval wrappers around the existing pgvector stores."""

from __future__ import annotations

import uuid
from typing import Any

from haystack import component

from app.db.clients import notes as notes_store, raw_inputs
from app.db.clients.raw_inputs import SimilarInput
from app.services.input.embedding import embed


@component
class RawInputSimilaritySearch:
    """Search stored raw-input embeddings without changing pgvector storage."""

    @component.output_types(hits=list)
    def run(
        self,
        session: Any,
        *,
        embedding: list[float],
        exclude_id: uuid.UUID,
        statuses: list[str],
        k: int,
    ) -> dict[str, list[SimilarInput]]:
        return {
            "hits": raw_inputs.search_similar(
                session,
                embedding=embedding,
                exclude_id=exclude_id,
                statuses=statuses,
                k=k,
            )
        }


@component
class NoteSearch:
    """Embed a note query with Gemini and search stored note embeddings."""

    @component.output_types(text=str)
    async def run(self, session: Any, *, query: str, k: int = 5) -> dict[str, str]:
        query = (query or "").strip()
        if not query:
            return {"text": "search_notes: empty query."}
        vec = await embed(query)
        if vec is None:
            return {"text": "search_notes: embeddings disabled — no notes available."}
        hits = notes_store.search_similar(session, embedding=vec, k=k)
        if not hits:
            return {"text": "search_notes: no matching notes."}
        lines = [f"- sim={h.similarity:.2f} | {h.content}" for h in hits]
        return {"text": "Notes:\n" + "\n".join(lines)}


def search_raw_inputs(
    session: Any,
    *,
    embedding: list[float],
    exclude_id: uuid.UUID,
    statuses: list[str],
    k: int,
) -> list[SimilarInput]:
    result = RawInputSimilaritySearch().run(
        session=session,
        embedding=embedding,
        exclude_id=exclude_id,
        statuses=statuses,
        k=k,
    )
    return result["hits"]


async def search_notes(session: Any, *, query: str, k: int = 5) -> str:
    result = await NoteSearch().run(session=session, query=query, k=k)
    return result["text"]
