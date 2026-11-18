"""Stage-2 search: the results shown when the user presses Enter.

One list, sorted by relevance across every source the filter allows:

  * local matches from the hybrid search (pgvector + keyword FTS + pg_trgm
    typo tolerance) over tasks, messages, notes and documents;
  * calendar events from the keyword suggest (plus Google Calendar itself
    under the events filter);
  * live Drive, Contacts and GitHub results.

Their own scores aren't comparable, so `rank.rank` re-scores every candidate
the same way — cosine similarity to the query (stored embeddings scored in
SQL, the rest embedded in one batch) fused by rank with a typo-tolerant word
match and the distance of the hit's date from now — and the list comes back
in that order.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.db.clients import search as search_client
from app.db.schemas.search import SearchHit
from app.services.github import search_hits as search_github_hits
from app.services.input.embedding import embed, embed_many
from app.services.search.calendar import search_calendar_live
from app.services.search.contacts import search_contacts
from app.services.search.drive import search_drive
from app.services.search.filters import ALL_CORPORA, Filters, corpus_restriction, parse_query
from app.services.search.rank import Candidate, cosine, rank, time_distance
from app.services.search.suggest import (
    attach_input_source_urls,
    drop_documents_shadowed_by_tasks,
    run_suggest,
)

log = logging.getLogger(__name__)

LOCAL_LIMIT = 30
SOURCE_LIMIT = 10
CANDIDATE_TEXT_CHARS = 500

# Filter kind → (local corpora, calendar events?, live providers).
KINDS: dict[str | None, tuple[frozenset[str], bool, tuple[str, ...]]] = {
    None: (ALL_CORPORA, True, ("drive", "contact", "github")),
    "tasks": (frozenset({"task"}), False, ()),
    "messages": (frozenset({"input"}), False, ()),
    "notes": (frozenset({"note"}), False, ()),
    "files": (frozenset({"document"}), False, ("drive",)),
    "events": (frozenset(), True, ("calendar",)),
    "contacts": (frozenset(), False, ("contact",)),
    "github": (frozenset(), False, ("github",)),
}


async def run_search(
    session: Session,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 40,
    source: str | None = None,
    label: str | None = None,
    drive_format: str | None = None,
    status: str | None = None,
    recurring: bool = False,
) -> list[SearchHit]:
    text, parsed = parse_query(query)
    if not text:
        return []
    filters = Filters(
        source=source or parsed.source,
        label=label or parsed.label,
        status=status or parsed.status,
        after=parsed.after,
        before=parsed.before,
    )
    corpora, with_events, providers = KINDS[kind]
    corpora = frozenset(corpora & (corpus_restriction(filters) or ALL_CORPORA))
    if recurring:
        corpora = frozenset(corpora & {"task"})

    live = asyncio.gather(*(_live(p, text, drive_format) for p in providers))
    try:
        embedding = await embed(text)
    except Exception:  # noqa: BLE001 — keyword and trigram matching still answer
        log.warning("search · query embedding failed; ranking by words only")
        embedding = None

    hits: list[SearchHit] = []
    if corpora:
        hits += _local(session, text, filters, corpora, embedding, recurring)
    if with_events:
        hits += run_suggest(session, query, limit=SOURCE_LIMIT, types=frozenset({"document"}),
                            source="calendar")
    for provider_hits in await live:
        hits += provider_hits

    now = datetime.now(timezone.utc)
    candidates = [
        Candidate(hit, _candidate_text(hit), distance=time_distance(hit, now, text))
        for hit in drop_duplicate_events(hits)
    ]
    if embedding is not None:
        await _score_semantic(session, embedding, candidates)
    return [c.hit for c in rank(text, candidates)][:limit]


def drop_duplicate_events(hits: list[SearchHit]) -> list[SearchHit]:
    """A cached event and its live copy are one occurrence with the same
    Calendar link (the cache keys rows by its own id, Google by the event id)."""
    seen: set[str] = set()
    kept = []
    for hit in hits:
        if hit.type == "document" and hit.source == "calendar" and hit.url:
            if hit.url in seen:
                continue
            seen.add(hit.url)
        kept.append(hit)
    return kept


def _local(session: Session, text: str, filters: Filters, corpora: frozenset[str],
           embedding: list[float] | None, recurring: bool) -> list[SearchHit]:
    settings = get_settings()
    found = search_client.hybrid_search(
        session,
        embedding=embedding,
        raw_text=text,
        k=LOCAL_LIMIT,
        corpora=corpora,
        min_input_chars=settings.min_input_chars,
        note_min_similarity=settings.notes_semantic_min_similarity,
        source=filters.source,
        label=filters.label,
        status=filters.status,
        before=filters.before,
        after=filters.after,
        recurring=recurring,
        trigram=True,
        note_origin=True,
    )
    result = drop_documents_shadowed_by_tasks([SearchHit.build(h) for h in found])
    attach_input_source_urls(session, result)
    return result


async def _live(provider: str, text: str, drive_format: str | None) -> list[SearchHit]:
    """One live provider, best effort: a failure or timeout is an empty list."""
    settings = get_settings()
    try:
        if provider == "github":
            return await search_github_hits(text, limit=SOURCE_LIMIT)
        with SessionLocal() as session:
            if provider == "calendar":
                return await search_calendar_live(
                    session, text, k=SOURCE_LIMIT, timeout=settings.search_calendar_timeout_seconds,
                )
            if provider == "drive":
                extra = {"mime_label": drive_format} if drive_format else {}
                return await search_drive(
                    session, text, k=SOURCE_LIMIT,
                    timeout=settings.search_drive_timeout_seconds, **extra,
                )
            return await search_contacts(
                session, text, k=SOURCE_LIMIT, timeout=settings.search_contacts_timeout_seconds,
            )
    except Exception:  # noqa: BLE001 — one provider never sinks the search
        log.exception("search · %s lookup failed", provider)
        return []


def _candidate_text(hit: SearchHit) -> str:
    return f"{hit.title}\n{hit.snippet or ''}"[:CANDIDATE_TEXT_CHARS]


async def _score_semantic(
    session: Session, embedding: list[float], candidates: list[Candidate]
) -> None:
    """Stored embeddings are scored in SQL; tasks and live results, which have
    none, are embedded in one batch call."""
    stored = search_client.stored_similarities(
        session, embedding, [(c.hit.type, c.hit.id) for c in candidates]
    )
    missing = []
    for c in candidates:
        similarity = stored.get((c.hit.type, c.hit.id))
        if similarity is None:
            missing.append(c)
        else:
            c.semantic = similarity
    if not missing:
        return
    try:
        vectors = await embed_many([c.text for c in missing])
    except Exception:  # noqa: BLE001 — those candidates rank by words alone
        log.warning("search · batch embedding failed for %d candidates", len(missing))
        return
    for c, vector in zip(missing, vectors):
        if vector is not None:
            c.semantic = cosine(embedding, vector)
