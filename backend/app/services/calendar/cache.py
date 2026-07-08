"""Mirror calendar events into the `documents` search cache.

Two entry points keep the cache current:

  * **Discovery** (`cache_calendar_events`) — every event the incremental sync
    surfaces is upserted; cancelled events are dropped. The backstop that heals
    any missed write-through within a minute.
  * **Write-through** (`write_through_event` / `forget_event`) — the app's own
    `create_event`/`patch_event`/`delete_event` update the cache synchronously,
    so an event the agent just created is searchable immediately, not a sync
    cycle later.

Our own managed events are skipped — task mirrors (`kind=task`) are already
represented in search by their task, and commute legs are ephemeral planner
artifacts; caching either would duplicate a task as a calendar event. Real
events (agent invitations, externally-created events) are cached. Past events
are kept (no time-based eviction). Embeddings are only recomputed when an
event's text actually changed, so re-syncs and time-only edits don't burn
embedding calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.clients import documents as documents_store
from app.services.calendar.client import CalendarEvent
from app.services.calendar.events import is_managed_event
from app.services.input.embedding import embed

log = logging.getLogger(__name__)


def _content(event: CalendarEvent) -> str:
    parts = [event.summary, event.location, event.description]
    return "\n".join(p.strip() for p in parts if p and p.strip()).strip()


def _snippet(event: CalendarEvent) -> str | None:
    if event.location:
        return event.location
    if event.description:
        return event.description[:200]
    return None


def _metadata(event: CalendarEvent) -> dict:
    meta = {
        "event_id": event.id,
        "calendar_id": event.calendar_id,
        "location": event.location,
        "all_day": event.all_day,
    }
    return {k: v for k, v in meta.items() if v is not None}


def _updated_at(event: CalendarEvent) -> datetime:
    raw = event.raw.get("updated")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _external_id(event: CalendarEvent) -> str:
    return f"{event.calendar_id}:{event.id}"


async def cache_one_event(session: Session, event: CalendarEvent) -> bool:
    """Upsert one event; return True if a fresh embedding was computed.

    Callers filter out commute legs (both entry points do) — this focuses on
    the upsert + embedding-dedup so it stays reusable."""
    external_id = _external_id(event)
    content = _content(event)
    existing = documents_store.get_by_external_id(
        session, provider="calendar", external_id=external_id
    )
    if existing is not None and existing.content == content and existing.embedding is not None:
        embedding = existing.embedding
        embedded = False
    else:
        embedding = await embed(content)
        embedded = embedding is not None

    documents_store.upsert(
        session,
        provider="calendar",
        external_id=external_id,
        title=event.summary,
        snippet=_snippet(event),
        content=content,
        url=event.html_link,
        metadata=_metadata(event),
        starts_at=event.start,
        ends_at=event.end,
        updated_at=_updated_at(event),
        embedding=embedding,
    )
    return embedded


async def cache_calendar_events(
    session: Session,
    *,
    active: Iterable[CalendarEvent],
    cancelled: Iterable[tuple[str, str]] = (),
) -> dict:
    """Upsert `active` events and drop `cancelled` (calendar_id, event_id) rows.

    Resilient per event: one embedding failure never aborts the batch (or the
    discovery pass that called it)."""
    upserted = embedded = forgotten = 0
    for event in active:
        if is_managed_event(event):
            # Never cache our own task/commute mirrors, and evict any cached
            # before this rule existed (the daily baseline re-surfaces them).
            if documents_store.delete(
                session, provider="calendar", external_id=_external_id(event)
            ):
                forgotten += 1
            continue
        try:
            if await cache_one_event(session, event):
                embedded += 1
            upserted += 1
        except Exception:  # noqa: BLE001 — best-effort cache; never break discovery
            log.exception("calendar cache · failed to cache event id=%s", event.id)
    for calendar_id, event_id in cancelled:
        if documents_store.delete(
            session, provider="calendar", external_id=f"{calendar_id}:{event_id}"
        ):
            forgotten += 1
    return {"upserted": upserted, "embedded": embedded, "forgotten": forgotten}


async def write_through_event(event: CalendarEvent) -> None:
    """Synchronously cache an event the app just created or patched.

    Runs in its own session so it never touches the caller's transaction, and
    swallows failures — discovery re-caches within a minute if this misses.
    Our own managed events (task mirrors + commute legs) are never cached — a
    task isn't duplicated as a calendar event — and any stale cache row is
    evicted (e.g. a task mirror cached before this rule, now being patched)."""
    if is_managed_event(event):
        forget_event(event.calendar_id, event.id)
        return
    session = SessionLocal()
    try:
        await cache_one_event(session, event)
        session.commit()
    except Exception:  # noqa: BLE001 — write-through must never break a calendar mutation
        log.exception("calendar write-through failed · event=%s", event.id)
        session.rollback()
    finally:
        session.close()


def forget_event(calendar_id: str, event_id: str) -> None:
    """Drop an event the app just deleted from the cache. Own session,
    best-effort (a no-op if it was never cached, e.g. a commute leg)."""
    session = SessionLocal()
    try:
        documents_store.delete(
            session, provider="calendar", external_id=f"{calendar_id}:{event_id}"
        )
        session.commit()
    except Exception:  # noqa: BLE001
        log.exception("calendar cache forget failed · event=%s", event_id)
        session.rollback()
    finally:
        session.close()
