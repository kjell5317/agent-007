"""Retrieval helpers over the existing pgvector stores.

Thin wrappers around `app.db.clients` so the agent flows don't reach into the
storage layer directly. No change to Gemini embeddings or pgvector storage.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.db.clients import documents as documents_store, notes as notes_store, raw_inputs
from app.db.clients.documents import CalendarMatch
from app.db.clients.raw_inputs import SimilarInput
from app.services.input.embedding import candidate_query_text, embed


def search_raw_inputs(
    session: Any,
    *,
    embedding: list[float],
    query: str,
    exclude_id: uuid.UUID,
    statuses: list[str],
    k: int,
) -> list[SimilarInput]:
    return raw_inputs.search_similar(
        session,
        embedding=embedding,
        query=query,
        exclude_id=exclude_id,
        statuses=statuses,
        k=k,
    )


def precedent_query_text(raw: Any) -> str:
    """Keyword-side query for the hybrid precedent lookup: the subject when we
    have one (the most discriminative short text), else the leading content.
    Kept short so `websearch_to_tsquery` doesn't AND together a whole body."""
    meta = getattr(raw, "source_metadata", None) or {}
    subject = (meta.get("subject") or "").strip()
    return (subject or (getattr(raw, "content", "") or "").strip())[:300]


async def related_calendar_events(
    session: Any, *, raw: Any, embedding: list[float] | None = None
) -> list[CalendarMatch]:
    """Upcoming cached calendar events that may relate to `raw`, for the task
    creation prompt. Reuses the input's stored embedding when given (same
    model and task type as the cached events), else embeds it here."""
    settings = get_settings()
    if embedding is None:
        embedding = await embed(
            candidate_query_text(raw.content or "", raw.source_metadata or {})
        )
    limit = settings.task_related_events_limit
    matches = documents_store.search_calendar_semantic(
        session,
        embedding=embedding,
        raw_text=precedent_query_text(raw),
        # Over-fetch: recurring instances share text and would fill every slot.
        k=limit * 4,
        min_similarity=settings.task_related_events_min_similarity,
        time_min=datetime.now(timezone.utc).isoformat(),
    )
    return soonest_per_event(matches)[:limit]


def calendar_match_ref(m: CalendarMatch) -> dict[str, Any]:
    return {
        "event_id": m.event_id,
        "calendar_id": m.calendar_id,
        "summary": m.summary,
        "starts_at": m.starts_at.isoformat() if m.starts_at else None,
        "similarity": round(m.similarity, 4),
    }


def soonest_per_event(matches: list[CalendarMatch]) -> list[CalendarMatch]:
    """Collapse same-titled events (recurring instances) to the soonest one,
    keeping the rank of the first occurrence."""
    best: dict[tuple[str | None, str], CalendarMatch] = {}
    for m in matches:
        key = (m.calendar_id, m.summary)
        kept = best.get(key)
        if kept is None or (
            m.starts_at is not None and (kept.starts_at is None or m.starts_at < kept.starts_at)
        ):
            best[key] = m
    return list(best.values())


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
    if h.updated_at is not None:
        parts.append(h.updated_at.date().isoformat())
    if h.source_from:
        parts.append(f"from: {h.source_from}")
    return "- " + " · ".join(parts) + f" | {h.content}"
