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
from app.services.calendar.events import (
    PROP_KIND,
    create_event,
    get_event,
    is_commute_event,
    is_managed_event,
    is_task_event,
    list_events_between,
    patch_event,
)
from app.services.notify import notify_calendar_event_updated

KIND_INVITATION = "invitation"
PROP_CREATED_BY = "created_by"
CREATED_BY_AGENT = "agent"


def _aware(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


async def run_find_calendar_events(session: Session, time_min: str, time_max: str) -> str:
    settings = get_settings()
    tz = ZoneInfo(settings.user_timezone)
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return "find_calendar_events: no calendar configured."
    start = parse_iso(time_min)
    end = parse_iso(time_max)
    if start is None or end is None:
        return "find_calendar_events: time_min and time_max must be ISO timestamps."
    start, end = _aware(start, tz), _aware(end, tz)
    if end <= start:
        return "find_calendar_events: time_max must be after time_min."
    events = await list_events_between(
        session, calendar_ids=[calendar_id], time_min=start, time_max=end,
    )
    if not events:
        return "find_calendar_events: no existing events in that window."
    lines = []
    for e in events:
        when = e.start.astimezone(tz).isoformat()
        loc = f" @ {e.location}" if e.location else ""
        lines.append(f"- id={e.id} | {when} | {e.summary}{loc}")
    return "Existing calendar events:\n" + "\n".join(lines)


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
