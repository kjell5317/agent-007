"""Shared `search_notes` non-terminal tool — used by both the new-input
agent and the extract-fields agent."""

from __future__ import annotations

from app.embeddings import embed
from app.storage import notes as notes_store


async def run_search_notes(session, query: str) -> str:
    """Embed the query, fetch top-k similar notes, format as text for the LLM."""
    query = (query or "").strip()
    if not query:
        return "search_notes: empty query."
    vec = await embed(query)
    if vec is None:
        return "search_notes: embeddings disabled — no notes available."
    hits = notes_store.search_similar(session, embedding=vec, k=5)
    if not hits:
        return "search_notes: no matching notes."
    lines = [f"- sim={h.similarity:.2f} | {h.content}" for h in hits]
    return "Notes:\n" + "\n".join(lines)
