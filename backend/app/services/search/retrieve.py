"""Stage-2 retrieval: one path used by BOTH the up-front per-message retrieval
and the `search` tool, so they behave identically.

A single freetext query fans out (with one embedding) across:
  * local hybrid RRF — tasks / inbox / notes / kotx docs (kotx links to its task)
  * federated calendar — semantic event search over the cached calendar
  * federated Drive — live `files.list`

`filters.source` narrows the fan-out: `drive`/`calendar` run only that backend;
any other source restricts the local search; no source runs all three. Metadata
filters (`before`/`after`/…) apply to every backend, including the API calls.
Calendar is served here (not via the local hybrid, which excludes it) so events
appear exactly once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import documents as documents_store
from app.db.clients import search as search_client
from app.db.clients import tasks as tasks_store
from app.db.models.raw_input import RawInput
from app.db.schemas.search import SearchHit
from app.services.input.embedding import embed
from app.services.search.drive import search_drive
from app.services.search.filters import Filters
from app.services.source_url import source_url_for_raw_input


async def retrieve(
    session: Session, query: str, *, filters: Filters | None = None
) -> list[SearchHit]:
    query = (query or "").strip()
    if not query:
        return []
    filters = filters or Filters()
    settings = get_settings()
    source = filters.source

    want_drive = source in (None, "drive")
    want_calendar = source in (None, "calendar")
    want_local = source is None or source not in ("drive", "calendar")

    embedding = None
    if want_local or want_calendar:
        try:
            embedding = await embed(query)
        except Exception:  # noqa: BLE001 — degrade to keyword-only on embed failure
            embedding = None

    hits: list[SearchHit] = []

    if want_local:
        raw = search_client.hybrid_search(
            session,
            embedding=embedding,
            raw_text=query,
            k=settings.search_chat_local_limit,
            min_input_chars=settings.min_input_chars,
            source=source,
            label=filters.label,
            status=filters.status,
            before=filters.before,
            after=filters.after,
        )
        local_hits = [SearchHit.build(h) for h in raw]
        _attach_input_source_urls(session, local_hits)
        hits.extend(local_hits)

    if want_calendar and embedding is not None:
        hits.extend(
            _calendar_hits(
                session,
                embedding=embedding,
                query=query,
                after=filters.after,
                before=filters.before,
                limit=settings.calendar_semantic_match_limit,
                min_sim=settings.calendar_semantic_min_similarity,
            )
        )

    if want_drive:
        hits.extend(
            await search_drive(
                session,
                query,
                k=settings.search_chat_drive_limit,
                timeout=settings.search_drive_timeout_seconds,
                after=filters.after,
                before=filters.before,
            )
        )

    return hits


def list_tasks(
    session: Session,
    *,
    status: str = "open",
    due_after: str | None = None,
    due_before: str | None = None,
    label: str | None = None,
    limit: int = 25,
) -> list[SearchHit]:
    """Structured task listing for agenda questions ("today's todos", "overdue",
    "due this week") — no keywords needed, unlike hybrid `retrieve`. Filters by
    derived status + a window on each task's effective date (scheduled_date, else
    due_date), ordered soonest-first, and returns citeable task hits."""
    tz = ZoneInfo(get_settings().user_timezone)
    lo = _day_bound(due_after, tz)
    hi = _day_bound(due_before, tz)
    want_label = (label or "").strip().lower() or None

    out: list[SearchHit] = []
    for task, derived in tasks_store.list_(session, status=status, limit=200):
        if want_label and (task.label or "").lower() != want_label:
            continue
        eff = task.scheduled_date or task.due_date
        if lo is not None and (eff is None or eff < lo):
            continue
        if hi is not None and (eff is None or eff >= hi):
            continue
        out.append(
            SearchHit(
                type="task",
                id=str(task.id),
                title=task.title,
                snippet=(task.description or "")[:200] or None,
                url=task.link,
                task_id=str(task.id),
                source=task.label,
                sender=None,
                status=derived,
                ts=eff,
                score=1.0,
            )
        )
        if len(out) >= limit:
            break
    return out


def _day_bound(value: str | None, tz: ZoneInfo) -> datetime | None:
    """Parse a `YYYY-MM-DD` (or full ISO) boundary into a tz-aware datetime for
    comparison against stored timestamps."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def _calendar_hits(
    session: Session,
    *,
    embedding: list[float],
    query: str,
    after: str | None,
    before: str | None,
    limit: int,
    min_sim: float,
) -> list[SearchHit]:
    matches = documents_store.search_calendar_semantic(
        session,
        embedding=embedding,
        raw_text=query,
        k=limit,
        min_similarity=min_sim,
        time_min=after,
        time_max=before,
    )
    return [
        SearchHit(
            type="document",
            id=m.event_id,
            title=m.summary,
            snippet=m.location,
            url=m.url,
            task_id=None,
            source="calendar",
            sender=None,
            status="event",
            ts=m.starts_at,
            score=float(m.similarity),
        )
        for m in matches
    ]


def _attach_input_source_urls(session: Session, hits: list[SearchHit]) -> None:
    """Give input hits a deep link to their source (mirrors run_suggest) so a
    citation can jump to the original thread."""
    for hit in hits:
        if hit.type != "input" or hit.url:
            continue
        try:
            raw = session.get(RawInput, uuid.UUID(hit.id))
        except ValueError:
            continue
        if raw is not None:
            hit.url = source_url_for_raw_input(raw)
