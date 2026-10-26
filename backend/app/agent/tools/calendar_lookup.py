"""Calendar lookup + creation/update tools for the new-input agent.

`find_calendar_events` lets the agent check the user's primary calendar for an
existing event before creating one — each result carries everything the agent
needs about the event, so no follow-up read is required; `create_event` adds an
event the user should see (an invitation, appointment, talk) without turning it
into a task. `update_event` patches a non-managed event already on the primary
calendar.

All are non-terminal: the runner appends their results to the conversation
and the agent continues to a terminal decision.

Agent-created events are tagged `kind=invitation` / `created_by=agent` but
deliberately NOT `managed_by=plan_service` — they sit at a fixed time and must
read as hard blockers to the planner, never as movable managed events.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.agent.helpers.text import parse_iso
from app.config import get_settings
from app.db.clients import documents as documents_store
from app.db.clients.documents import CalendarMatch
from app.services.calendar.cache import EVENT_DETAIL_CHARS, event_metadata
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
        # The language-neutral keyword branch works even without embeddings.
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

    if start is None or end is None:
        return "find_calendar_events: provide `query`, or both `time_min` and `time_max`."
    if end <= start:
        return "find_calendar_events: time_max must be after time_min."
    events = await list_events_between(
        session, calendar_ids=[calendar_id], time_min=start, time_max=end,
    )
    if not events:
        return "find_calendar_events: no existing events in that window."
    lines = [_event_line(e.id, e.summary, e.start, e.end, event_metadata(e), e.description, tz)
             for e in events[:10]]
    omitted = f"\n({len(events) - 10} more events omitted)" if len(events) > 10 else ""
    return "Existing calendar events:\n" + "\n".join(lines) + omitted


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


def event_when(
    start: datetime | None, end: datetime | None, all_day: bool, tz: ZoneInfo
) -> str:
    if start is None:
        return "(no time)"
    if all_day:
        # All-day bounds are midnight-UTC dates with an exclusive end.
        first = start.astimezone(timezone.utc).date()
        last = (end.astimezone(timezone.utc) - timedelta(days=1)).date() if end else first
        span = first.isoformat() if last <= first else f"{first.isoformat()} to {last.isoformat()}"
        return f"{span} all day"
    local_start = start.astimezone(tz)
    if end is None:
        return local_start.isoformat()
    local_end = end.astimezone(tz)
    until = (
        local_end.strftime("%H:%M") if local_end.date() == local_start.date()
        else local_end.isoformat()
    )
    return f"{local_start.isoformat()} to {until}"


def event_facts(meta: Mapping[str, Any]) -> list[str]:
    """Who/how/whether facts about an event, from its `event_metadata`."""
    facts: list[str] = []
    # All-day events are free by default, so the flag only says something on timed ones.
    if meta.get("free") and not meta.get("all_day"):
        facts.append("shown as free")
    if meta.get("rsvp"):
        facts.append(f"user's rsvp: {meta['rsvp']}")
    if meta.get("organizer"):
        facts.append(f"organizer: {meta['organizer']}")
    if meta.get("attendees"):
        facts.append("attendees: " + ", ".join(meta["attendees"]))
    if meta.get("meeting_url"):
        facts.append(f"meeting: {meta['meeting_url']}")
    return facts


def _match_metadata(m: CalendarMatch) -> dict[str, Any]:
    return {**m.meta, "calendar_id": m.calendar_id, "location": m.location, "label_id": m.label_id}


def _event_line(
    event_id: str,
    summary: str,
    start: datetime | None,
    end: datetime | None,
    meta: Mapping[str, Any],
    description: str | None,
    tz: ZoneInfo,
    *,
    detail_limit: int = EVENT_DETAIL_CHARS,
) -> str:
    location = meta.get("location")
    loc = f" @ {location}" if location else ""
    calendar = f" | calendar_id={meta['calendar_id']}" if meta.get("calendar_id") else ""
    facts = "".join(f" | {fact}" for fact in event_facts(meta))
    detail = (description or "").strip()
    # Cached content starts with summary and location. They are already
    # displayed in the event line, so don't spend context on them twice.
    for prefix in (summary, location):
        if prefix and detail.startswith(prefix):
            detail = detail[len(prefix):].lstrip("\n ")
    details = f" | details={detail[:detail_limit]}" if detail else ""
    if len(detail) > detail_limit:
        details += " [truncated]"
    when = event_when(start, end, bool(meta.get("all_day")), tz)
    return f"- id={event_id}{calendar} | {when} | {summary}{loc}{facts}{details}"


def related_events_section(matches: Sequence[CalendarMatch], tz: ZoneInfo) -> list[str]:
    """Prompt lines listing upcoming events the input may prepare for."""
    if not matches:
        return []
    lines = [
        "",
        "Upcoming calendar events that may be related (retrieved by similarity; "
        "often none of them is). If the task prepares for one of them, link it "
        "with `related_event_id` + `related_event_calendar_id` and set a due_date "
        "before it starts with realistic lead time. Ignore events that don't "
        "clearly match.",
    ]
    for m in matches:
        lines.append(_event_line(m.event_id, m.summary, m.starts_at, m.ends_at,
                                 _match_metadata(m), m.description, tz, detail_limit=300))
    return lines


def _format_matches(query: str, matches: Sequence[CalendarMatch], tz: ZoneInfo) -> str:
    if not matches:
        return f"find_calendar_events: no cached events matching '{query}'."
    lines = [
        _event_line(m.event_id, m.summary, m.starts_at, m.ends_at,
                    _match_metadata(m), m.description, tz)
        + f" | sim={m.similarity:.2f}"
        for m in matches
    ]
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
        f"create_event: added '{summary}' at {start_dt.astimezone(tz).isoformat()} "
        f"with id={event.id} calendar_id={calendar_id}.",
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
