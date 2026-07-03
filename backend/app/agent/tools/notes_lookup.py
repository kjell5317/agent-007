"""Shared note tooling: the `search_notes` non-terminal tool and the
`save_notes` persistence helper. Every agent flow (new-input, thread
follow-up, extract-fields) reads and writes long-term memory through these."""

from __future__ import annotations

from app.agent.retrieval import search_notes
from app.db.clients import notes as notes_store
from app.services.input.embedding import embed


async def run_search_notes(session, query: str) -> str:
    """Embed the query, fetch top-k similar notes, format as text for the LLM."""
    return await search_notes(session, query=query, k=5)


async def save_notes(session, raw_input_id, raw_notes) -> list[str]:
    """Persist the notes an agent attached to a terminal tool call. Each note
    is embedded so future `search_notes` calls can retrieve it. Returns the
    list of saved note contents (for the trace)."""
    saved: list[str] = []
    if not isinstance(raw_notes, list):
        return saved
    for entry in raw_notes:
        content = str(entry or "").strip()
        if not content:
            continue
        vec = await embed(content)
        notes_store.create(
            session,
            content=content,
            source_raw_input_id=raw_input_id,
            embedding=vec,
        )
        saved.append(content)
    if saved:
        session.commit()
    return saved
