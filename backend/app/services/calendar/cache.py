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
events (agent invitations, externally-created events) are cached, show-as-free
ones included — the scheduler ignores those, but a task can still prepare for
one (an all-day trip marker is free by default). Past events
are kept (no time-based eviction). Embeddings are only recomputed when an
event's text actually changed, so re-syncs and time-only edits don't burn
embedding calls. Each upsert also re-derives the event's work type and effort
(see `services.calendar.attributes`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.clients import documents as documents_store
from app.services.calendar.client import CalendarEvent
from app.services.calendar.events import is_free_event, is_managed_event
from app.services.input.embedding import embed

if TYPE_CHECKING:
    from app.services.calendar.attributes import LabelCatalog

log = logging.getLogger(__name__)

# How much of an event's description the agents see per event. Longer ones are
# marked truncated so an agent never patches an event from a clipped copy.
EVENT_DETAIL_CHARS = 4000

# A large invite's guest list is capped; the agent needs who, not a roster.
_ATTENDEE_LIMIT = 20


def _content(event: CalendarEvent) -> str:
    parts = [event.summary, event.location, event.description]
    return "\n".join(p.strip() for p in parts if p and p.strip()).strip()


def _snippet(event: CalendarEvent) -> str | None:
    # Display/preview text (chat context + citations). The location lives in
    # `content` for search; the snippet leads with the title (the summary) and a
    # truncated description so a preview reads like the event, not its address.
    parts = [event.summary, (event.description or "")[:200]]
    snippet = " — ".join(p.strip() for p in parts if p and p.strip())
    return snippet or None


def event_metadata(event: CalendarEvent) -> dict:
    """The event's facts beyond title/time/description — cached as the
    document's metadata and built the same way for live calendar reads, so
    both lookup paths describe an event identically."""
    meta = {
        "event_id": event.id,
        "calendar_id": event.calendar_id,
        "location": event.location,
        "all_day": event.all_day,
        "free": is_free_event(event),
        "label_id": event.raw.get("eventLabelId"),
        "organizer": _organizer(event),
        "attendees": _attendees(event) or None,
        "meeting_url": _meeting_url(event),
        "rsvp": _rsvp(event),
    }
    return {k: v for k, v in meta.items() if v is not None}


def _organizer(event: CalendarEvent) -> str | None:
    organizer = event.raw.get("organizer")
    if not isinstance(organizer, dict) or organizer.get("self"):
        return None
    return organizer.get("displayName") or organizer.get("email")


def _attendees(event: CalendarEvent) -> list[str]:
    attendees = event.raw.get("attendees")
    if not isinstance(attendees, list):
        return []
    # Rooms are listed as attendees too (`resource`); `self` is the user.
    names = [
        attendee.get("displayName") or attendee["email"]
        for attendee in attendees
        if isinstance(attendee, dict) and attendee.get("email")
        and not attendee.get("self") and not attendee.get("resource")
    ]
    if len(names) > _ATTENDEE_LIMIT:
        return [*names[:_ATTENDEE_LIMIT], f"+{len(names) - _ATTENDEE_LIMIT} more"]
    return names


def _meeting_url(event: CalendarEvent) -> str | None:
    if event.raw.get("hangoutLink"):
        return event.raw["hangoutLink"]
    conference = event.raw.get("conferenceData") or {}
    return next(
        (
            entry.get("uri")
            for entry in conference.get("entryPoints") or []
            if entry.get("entryPointType") == "video"
        ),
        None,
    )


def _rsvp(event: CalendarEvent) -> str | None:
    """The user's response when it isn't a plain accept (declined, tentative,
    needsAction); None for events without guests."""
    for attendee in event.raw.get("attendees") or []:
        if isinstance(attendee, dict) and attendee.get("self"):
            status = attendee.get("responseStatus")
            return status if status and status != "accepted" else None
    return None


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


async def cache_one_event(
    session: Session, event: CalendarEvent, *, labels: LabelCatalog | None = None
) -> bool:
    """Upsert one event; return True if a fresh embedding was computed.

    Callers filter out commute legs (both entry points do) — this focuses on
    the upsert + embedding-dedup so it stays reusable. `labels` saves a batch
    caller the per-event label lookups."""
    external_id = _external_id(event)
    content = _content(event)
    existing = documents_store.get_by_external_id(
        session, provider="calendar", external_id=external_id
    )
    content_changed = existing is None or existing.content != content
    # Imported here: calendar.attributes → the attribute classifier → the agent
    # package, whose calendar tools import this module.
    from app.services.calendar.attributes import cached_attributes, label_catalog

    label_defaults, label_names = labels or label_catalog(session)
    attributes = cached_attributes(
        event,
        existing.meta if existing is not None else None,
        content_changed=content_changed,
        label_defaults=label_defaults,
        label_names=label_names,
    )
    if existing is not None and not content_changed and existing.embedding is not None:
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
        metadata={**event_metadata(event), **attributes},
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
    labels: LabelCatalog | None = None
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
            if labels is None:
                from app.services.calendar.attributes import label_catalog

                labels = label_catalog(session)
            if await cache_one_event(session, event, labels=labels):
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
