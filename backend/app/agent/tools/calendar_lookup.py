"""Calendar lookup + creation/update tools for the new-input agent.

`find_calendar_events` lets the agent check the user's primary calendar for an
existing event before creating one; `create_event` adds an event the user
should see (an invitation, appointment, talk) without turning it into a task.
`update_event` patches a non-managed event already on the primary calendar.

All are non-terminal: the runner appends their results to the conversation
and the agent continues to a terminal decision.

Agent-created events are tagged `kind=invitation` / `created_by=agent` but
deliberately NOT `managed_by=plan_service` — they sit at a fixed time and must
read as hard blockers to the planner, never as movable managed events.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.agent.helpers.text import parse_iso
from app.config import get_settings
from app.db.clients import documents as documents_store
from app.services.calendar.events import (
    PROP_KIND,
    create_event,
    delete_event,
    get_event,
    is_commute_event,
    is_managed_event,
    is_task_event,
    list_events_between,
    patch_event,
)
from app.services.input.embedding import embed
from app.services.notify import notify_calendar_event_updated

KIND_INVITATION = "invitation"
PROP_CREATED_BY = "created_by"
CREATED_BY_AGENT = "agent"


def _aware(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


async def run_find_calendar_events(
    session: Session,
    *,
    time_min: str | None = None,
    time_max: str | None = None,
    query: str | None = None,
) -> str:
    """Find events either by time window (live calendar read) or by `query`
    (hybrid keyword + semantic search over the cached calendar events discovery
    mirrors in). A `query` matches both by term and by meaning — "team offsite"
    finds "Q3 offsite planning" — and returns only upcoming events (anything
    starting before now is excluded). An optional time window narrows further."""
    settings = get_settings()
    tz = ZoneInfo(settings.user_timezone)
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return "find_calendar_events: no calendar configured."

    start = _parse_bound(time_min, tz)
    end = _parse_bound(time_max, tz)
    if start is False or end is False:
        return "find_calendar_events: time_min and time_max must be ISO timestamps."

    q = (query or "").strip()
    if q:
        embedding = await embed(q)
        if embedding is not None:
            # Default the window start to now so past events (starting before
            # now) drop out; an explicit time_min from the agent still wins.
            floor = start or datetime.now(tz)
            matches = documents_store.search_calendar_semantic(
                session,
                embedding=embedding,
                raw_text=q,
                k=settings.calendar_semantic_match_limit,
                min_similarity=settings.calendar_semantic_min_similarity,
                time_min=floor.isoformat(),
                time_max=end.isoformat() if end else None,
            )
            return _format_matches(q, matches, tz)
        # No embeddings configured: fall back to a window listing if one was
        # given, otherwise say so.
        if start is None or end is None:
            return "find_calendar_events: semantic search needs embeddings configured."

    if start is None or end is None:
        return "find_calendar_events: provide `query`, or both `time_min` and `time_max`."
    if end <= start:
        return "find_calendar_events: time_max must be after time_min."
    events = await list_events_between(
        session, calendar_ids=[calendar_id], time_min=start, time_max=end,
    )
    if not events:
        return "find_calendar_events: no existing events in that window."
    lines = [_event_line(e.id, e.start.astimezone(tz).isoformat(), e.summary, e.location)
             for e in events]
    return "Existing calendar events:\n" + "\n".join(lines)


def _parse_bound(value: str | None, tz: ZoneInfo) -> datetime | bool | None:
    """None when absent, False when malformed, else an aware datetime."""
    if not value:
        return None
    try:
        parsed = parse_iso(value)
    except ValueError:
        return False
    if parsed is None:
        return False
    return _aware(parsed, tz)


def _event_line(event_id: str, when: str, summary: str, location: str | None) -> str:
    loc = f" @ {location}" if location else ""
    return f"- id={event_id} | {when} | {summary}{loc}"


def _format_matches(query: str, matches, tz: ZoneInfo) -> str:
    if not matches:
        return f"find_calendar_events: no cached events matching '{query}'."
    lines = []
    for m in matches:
        when = m.starts_at.astimezone(tz).isoformat() if m.starts_at else "(no time)"
        lines.append(
            _event_line(m.event_id, when, m.summary, m.location) + f" | sim={m.similarity:.2f}"
        )
    return f"Cached calendar events matching '{query}':\n" + "\n".join(lines)


async def run_create_event(
    session: Session,
    *,
    summary: str,
    start: str,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> tuple[str, str | None]:
    """Create the event; return (message_for_llm, event_id_or_None)."""
    settings = get_settings()
    tz = ZoneInfo(settings.user_timezone)
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return "create_event: no calendar configured — event not created.", None
    if not summary.strip():
        return "create_event: `summary` is required.", None
    start_dt = parse_iso(start)
    if start_dt is None:
        return "create_event: `start` is required.", None
    start_dt = _aware(start_dt, tz)
    parsed_end = parse_iso(end) if end else None
    end_dt = _aware(parsed_end, tz) if parsed_end else (
        start_dt + timedelta(minutes=settings.google_calendar_default_event_minutes)
    )
    event = await create_event(
        session,
        calendar_id=calendar_id,
        summary=summary,
        start=start_dt,
        end=end_dt,
        description=description,
        location=location,
        private_properties={PROP_KIND: KIND_INVITATION, PROP_CREATED_BY: CREATED_BY_AGENT},
    )
    return (
        f"create_event: added '{summary}' at {start_dt.astimezone(tz).isoformat()}.",
        event.id,
    )


async def run_update_event(
    session: Session,
    *,
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
) -> tuple[str, str | None]:
    """Patch the event; return (message_for_llm, updated_event_id_or_None)."""
    settings = get_settings()
    tz = ZoneInfo(settings.user_timezone)
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return "update_event: no calendar configured — event not updated.", None

    event_id = (event_id or "").strip()
    if not event_id or any(ch.isspace() for ch in event_id) or "/" in event_id:
        return "update_event: `event_id` must be a valid calendar event id.", None

    existing = await get_event(session, calendar_id=calendar_id, event_id=event_id)
    if is_managed_event(existing):
        kind = (
            "task"
            if is_task_event(existing)
            else "commute"
            if is_commute_event(existing)
            else "managed"
        )
        return (
            f"update_event: {kind} calendar events are managed by the task planner; "
            "use `update_task` for task-related changes instead.",
            None,
        )

    start_dt = _parse_optional_datetime(start, tz)
    end_dt = _parse_optional_datetime(end, tz)
    if start_dt is False or end_dt is False:
        return "update_event: `start` and `end` must be ISO timestamps when supplied.", None

    patch_kwargs = _event_patch_kwargs(
        existing_start=existing.start,
        existing_end=existing.end,
        summary=summary,
        start=start_dt,
        end=end_dt,
        description=description,
        location=location,
    )
    if not patch_kwargs:
        return "update_event: no fields supplied to update.", None

    effective_start = patch_kwargs.get("start", existing.start)
    effective_end = patch_kwargs.get("end", existing.end)
    if effective_end <= effective_start:
        return "update_event: `end` must be after `start`.", None

    updated = await patch_event(
        session,
        calendar_id=calendar_id,
        event_id=event_id,
        **patch_kwargs,
    )
    await notify_calendar_event_updated(updated)
    return (
        (
            "update_event: updated "
            f"'{updated.summary}' at {updated.start.astimezone(tz).isoformat()}."
        ),
        updated.id,
    )


async def run_delete_event(session: Session, *, event_id: str) -> tuple[str, str | None]:
    """Delete a non-managed event; return (message_for_llm, deleted_event_id_or_None).
    Task/commute events are planner-managed and refused, mirroring `run_update_event`."""
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return "delete_event: no calendar configured — nothing deleted.", None

    event_id = (event_id or "").strip()
    if not event_id or any(ch.isspace() for ch in event_id) or "/" in event_id:
        return "delete_event: `event_id` must be a valid calendar event id.", None

    existing = await get_event(session, calendar_id=calendar_id, event_id=event_id)
    if is_managed_event(existing):
        kind = (
            "task"
            if is_task_event(existing)
            else "commute"
            if is_commute_event(existing)
            else "managed"
        )
        return (
            f"delete_event: {kind} calendar events are managed by the task planner; "
            "use `update_task` to close a task instead.",
            None,
        )

    await delete_event(session, calendar_id=calendar_id, event_id=event_id)
    return f"delete_event: deleted '{existing.summary}'.", event_id


def _parse_optional_datetime(value: str | None, tz: ZoneInfo) -> datetime | bool | None:
    if value is None:
        return None
    try:
        parsed = parse_iso(value)
    except ValueError:
        return False
    if parsed is None:
        return False
    return _aware(parsed, tz)


def _event_patch_kwargs(
    *,
    existing_start: datetime,
    existing_end: datetime,
    summary: str | None,
    start: datetime | None,
    end: datetime | None,
    description: str | None,
    location: str | None,
) -> dict:
    patch_kwargs: dict = {}
    if summary is not None:
        patch_kwargs["summary"] = summary
    if start is not None:
        patch_kwargs["start"] = start
        if end is None:
            patch_kwargs["end"] = start + (existing_end - existing_start)
    if end is not None:
        patch_kwargs["end"] = end
    if description is not None:
        patch_kwargs["description"] = description
    if location is not None:
        patch_kwargs["location"] = location
    return patch_kwargs
