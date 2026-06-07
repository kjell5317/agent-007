"""Calendar lookup + creation tools for the new-input agent.

`find_calendar_events` lets the agent check the user's primary calendar for an
existing event before creating one; `create_event` adds an event the user
should see (an invitation, appointment, talk) without turning it into a task.

Both are non-terminal: the runner appends their results to the conversation
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
    list_events_between,
)

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
        lines.append(f"- {when} | {e.summary}{loc}")
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
