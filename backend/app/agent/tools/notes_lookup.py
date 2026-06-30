"""Shared `search_notes` non-terminal tool — used by both the new-input
agent and the extract-fields agent."""

from __future__ import annotations

from app.agent.retrieval import search_notes


async def run_search_notes(session, query: str) -> str:
    """Embed the query, fetch top-k similar notes, format as text for the LLM."""
    return await search_notes(session, query=query, k=5)
