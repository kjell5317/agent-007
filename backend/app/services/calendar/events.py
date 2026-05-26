"""Calendar event operations.

Two layers in one file:

  * **Generic API ops** (`list_week_events`, `create_event`, `patch_event`,
    `delete_event`) — operate on raw event fields. Used by commute
    planning and by anything that creates non-task events.

  * **Task-mirror helpers** (`add_task_event`, `update_task_event`,
    `delete_task_event`) — translate a `Task` row into the right
    payload and call the generic ops. Pure CRUD: no planning happens
    here, callers supply `(start, end)`. The planning service is the
    intended caller; today's callers wrap `plan_task_slot` around
    these directly.

Auth piggybacks on the same Google OAuth bundle the Gmail source uses;
the required scope (`calendar.events`) lives in `app.auth.google`. The
caller hands in a Session and (optionally) an `account_key`; this
module handles token refresh and returns normalized `CalendarEvent`
rows for the generic ops, or `None` for the task-mirror ops (which
mutate `task.calendar_event_id` as their side effect).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.labels import color_for
from app.services.calendar.client import (
    CalendarEvent,
    authorized_client,
    normalize,
    rfc3339,
    tz_name,
)

log = logging.getLogger(__name__)

WINDOW_DAYS = 7


# --- Generic API ops ---------------------------------------------------------


async def list_week_events(
    session: Session,
    *,
    calendar_ids: Iterable[str],
    week_start: datetime,
    account_key: str | None = None,
) -> list[CalendarEvent]:
    """Return every event in `[week_start, week_start + 7d)` across calendars.

    Pass any timezone-aware datetime as `week_start` — the window shifts with
    it (so "this week", "next week", or any arbitrary anchor are all just
    different `week_start` values).
    """
    if week_start.tzinfo is None:
        raise ValueError("week_start must be timezone-aware")

    ids = list(calendar_ids)
    if not ids:
        return []

    time_min = week_start
    time_max = week_start + timedelta(days=WINDOW_DAYS)

    client = await authorized_client(session, account_key)
    events: list[CalendarEvent] = []
    for cid in ids:
        log.info(
            "calendar list · id=%s window=%s..%s",
            cid, time_min.isoformat(), time_max.isoformat(),
        )
        items = await client.list_events(cid, time_min=time_min, time_max=time_max)
        events.extend(normalize(it, cid) for it in items)
    events.sort(key=lambda e: e.start)
    return events


async def create_event(
    session: Session,
    *,
    calendar_id: str,
    summary: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
    location: str | None = None,
    color_id: str | None = None,
    account_key: str | None = None,
) -> CalendarEvent:
    """Create an event on `calendar_id`. `start`/`end` must be tz-aware."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")

    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": rfc3339(start), "timeZone": tz_name(start)},
        "end": {"dateTime": rfc3339(end), "timeZone": tz_name(end)},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if color_id:
        body["colorId"] = color_id

    client = await authorized_client(session, account_key)
    log.info("calendar insert · calendar=%s summary=%r", calendar_id, summary)
    raw = await client.insert_event(calendar_id, body)
    return normalize(raw, calendar_id)


async def patch_event(
    session: Session,
    *,
    calendar_id: str,
    event_id: str,
    summary: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    description: str | None = None,
    location: str | None = None,
    color_id: str | None = None,
    account_key: str | None = None,
) -> CalendarEvent:
    """Patch an existing event on `calendar_id`. Only provided fields change."""
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        body["start"] = {"dateTime": rfc3339(start), "timeZone": tz_name(start)}
    if end is not None:
        if end.tzinfo is None:
            raise ValueError("end must be timezone-aware")
        body["end"] = {"dateTime": rfc3339(end), "timeZone": tz_name(end)}
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if color_id is not None:
        body["colorId"] = color_id

    client = await authorized_client(session, account_key)
    log.info("calendar patch · calendar=%s event=%s", calendar_id, event_id)
    raw = await client.patch_event(calendar_id, event_id, body)
    return normalize(raw, calendar_id)


async def delete_event(
    session: Session,
    *,
    calendar_id: str,
    event_id: str,
    account_key: str | None = None,
) -> None:
    """Delete an event on `calendar_id`."""
    client = await authorized_client(session, account_key)
    log.info("calendar delete · calendar=%s event=%s", calendar_id, event_id)
    await client.delete_event(calendar_id, event_id)


# --- Task-mirror helpers -----------------------------------------------------


async def add_task_event(
    session: Session,
    task,
    *,
    start: datetime,
    end: datetime,
) -> None:
    """Create the calendar mirror for `task` spanning `[start, end)`.

    Persists the new event id on `task.calendar_event_id`. No-op when no
    calendar is configured. Best-effort: calendar failures are logged and
    swallowed so they can't break task creation.
    """
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    try:
        event = await create_event(
            session,
            calendar_id=calendar_id,
            summary=task.title,
            start=start,
            end=end,
            description=_task_description(task),
            location=task.location,
            color_id=color_for(task.label),
        )
    except Exception as exc:  # noqa: BLE001 — never let calendar break task creation
        log.warning("calendar add failed · task=%s err=%s", task.id, exc)
        return

    task.calendar_event_id = event.id
    session.commit()


async def update_task_event(
    session: Session,
    task,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    changed_fields: set[str] | None = None,
) -> None:
    """Patch the task's calendar event.

    `start` / `end` are optional. When omitted, the event keeps its
    current Google-side times — useful for "I only renamed the task"
    edits where re-planning a slot would be wasteful.

    `changed_fields` is the set of task field names the caller actually
    changed (e.g. `{"title"}`). When provided we patch only the matching
    calendar fields, so a rename doesn't also rewrite description or
    location. Pass `None` to push every task-mirrored field.

    No-op when no calendar is configured. If the task has no mirrored
    event yet, fall back to `add_task_event` — but only when both
    `start` and `end` are supplied (without them, we can't create an
    event). Best-effort: calendar failures are logged and swallowed.
    """
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    if not task.calendar_event_id:
        if start is None or end is None:
            log.warning("update_task_event · task=%s has no event yet, skipping", task.id)
            return
        await add_task_event(session, task, start=start, end=end)
        return

    def _changed(name: str) -> bool:
        return changed_fields is None or name in changed_fields

    patch_kwargs: dict[str, Any] = {}
    if _changed("title"):
        patch_kwargs["summary"] = task.title
    if _changed("description") or _changed("link"):
        patch_kwargs["description"] = _task_description(task) or ""
    if _changed("location"):
        patch_kwargs["location"] = task.location or ""
    if _changed("label"):
        patch_kwargs["color_id"] = color_for(task.label)
    if start is not None:
        patch_kwargs["start"] = start
    if end is not None:
        patch_kwargs["end"] = end

    if not patch_kwargs:
        log.debug("update_task_event · task=%s no fields to patch", task.id)
        return

    try:
        await patch_event(
            session,
            calendar_id=calendar_id,
            event_id=task.calendar_event_id,
            **patch_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — never let calendar break task updates
        log.warning("calendar update failed · task=%s err=%s", task.id, exc)


async def delete_task_event(session: Session, task) -> None:
    """Drop the task's calendar mirror. No-op when nothing is mirrored or no
    calendar is configured. Clears `task.calendar_event_id` on success so a
    later re-open creates a fresh event. Best-effort."""
    if not task.calendar_event_id:
        return
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    try:
        await delete_event(
            session, calendar_id=calendar_id, event_id=task.calendar_event_id,
        )
    except Exception as exc:  # noqa: BLE001 — never let calendar break task state changes
        log.warning("calendar delete failed · task=%s err=%s", task.id, exc)
        return

    task.calendar_event_id = None
    session.commit()


def _task_description(task) -> str | None:
    parts: list[str] = []
    if task.description:
        parts.append(task.description)
    if task.link:
        parts.append(task.link)
    return "\n\n".join(parts) or None
