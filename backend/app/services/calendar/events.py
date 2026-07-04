"""Calendar event operations.

Two layers in one file:

  * **Generic API ops** (`list_events_between`, `create_event`, `patch_event`,
    `delete_event`) — operate on raw event fields. Used by commute planning
    and by anything that creates non-task events.

  * **Task-mirror helpers** (`add_task_event`, `update_task_event`,
    `delete_task_event`) — translate a `Task` row into the right
    payload and call the generic ops. Pure CRUD: no planning happens
    here, callers supply `(start, end)`. The planning service is the
    intended caller.

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

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import tasks as tasks_store
from app.labels import color_for
from app.services.location import resolve_location_alias
from app.services.calendar.client import (
    CalendarEvent,
    authorized_client,
    normalize,
    rfc3339,
    tz_name,
)

log = logging.getLogger(__name__)

WINDOW_DAYS = 7
MANAGED_BY = "plan_service"
PROP_MANAGED_BY = "managed_by"
PROP_KIND = "kind"
KIND_TASK = "task"
KIND_COMMUTE = "commute"


# --- Generic API ops ---------------------------------------------------------


async def list_events_between(
    session: Session,
    *,
    calendar_ids: Iterable[str],
    time_min: datetime,
    time_max: datetime,
    account_key: str | None = None,
) -> list[CalendarEvent]:
    """Return every event in `[time_min, time_max)` across calendars."""
    if time_min.tzinfo is None or time_max.tzinfo is None:
        raise ValueError("time_min and time_max must be timezone-aware")
    if time_max <= time_min:
        raise ValueError("time_max must be after time_min")

    ids = list(calendar_ids)
    if not ids:
        return []

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
    private_properties: dict[str, str] | None = None,
    reminders: dict | None = None,
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
        body["location"] = resolve_location_alias(location)
    if color_id:
        body["colorId"] = color_id
    if private_properties:
        body["extendedProperties"] = {"private": _clean_private_properties(private_properties)}
    if reminders is not None:
        body["reminders"] = reminders

    client = await authorized_client(session, account_key)
    log.info("calendar insert · calendar=%s summary=%r", calendar_id, summary)
    raw = await client.insert_event(calendar_id, body)
    return normalize(raw, calendar_id)


async def get_event(
    session: Session,
    *,
    calendar_id: str,
    event_id: str,
    account_key: str | None = None,
) -> CalendarEvent:
    """Fetch one event by id."""
    client = await authorized_client(session, account_key)
    raw = await client.get_event(calendar_id, event_id)
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
    private_properties: dict[str, str] | None = None,
    reminders: dict | None = None,
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
        body["location"] = resolve_location_alias(location) or ""
    if color_id is not None:
        body["colorId"] = color_id
    if private_properties is not None:
        body["extendedProperties"] = {"private": _clean_private_properties(private_properties)}
    if reminders is not None:
        body["reminders"] = reminders

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
    calendar is configured. Raises on calendar API failure so the planner
    can surface the problem rather than firing a "Scheduled" notification
    for an event that never got created.
    """
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    # Default to a popup before the task; the commute planner silences it
    # when an arriving leg takes over the notification.
    event = await create_event(
        session,
        calendar_id=calendar_id,
        summary=task.title,
        start=start,
        end=end,
        description=_task_description(task),
        location=resolve_location_alias(task.location),
        color_id=color_for(task.label),
        private_properties=task_private_properties(task),
        reminders=popup_reminders(settings.reminder_lead_minutes),
    )
    tasks_store.set_schedule(
        session,
        task,
        event_id=event.id,
        scheduled_date=event.start,
    )
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
    event). Raises on calendar API failure (other than 404/410, which is
    treated as a stale-id and recovered via re-create).
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
        patch_kwargs["location"] = resolve_location_alias(task.location) or ""
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
        event = await patch_event(
            session,
            calendar_id=calendar_id,
            event_id=task.calendar_event_id,
            private_properties=task_private_properties(task),
            **patch_kwargs,
        )
        if start is not None:
            tasks_store.set_schedule(
                session,
                task,
                event_id=event.id,
                scheduled_date=event.start,
            )
            session.commit()
    except httpx.HTTPStatusError as exc:
        # 404 / 410 = the event we thought we owned is gone (deleted in
        # Google Calendar UI, or never created successfully). Drop the
        # stale id and re-create from scratch if we have a slot.
        if exc.response.status_code in (404, 410) and start is not None and end is not None:
            log.info(
                "calendar update · task=%s event=%s missing (status=%s); recreating",
                task.id, task.calendar_event_id, exc.response.status_code,
            )
            tasks_store.clear_calendar_event(session, task)
            session.commit()
            await add_task_event(session, task, start=start, end=end)
            return
        raise


async def delete_task_event(session: Session, task) -> None:
    """Drop the task's calendar mirror and any commute legs anchored to it.
    No-op when nothing is mirrored or no calendar is configured. Clears
    `task.calendar_event_id` on success so a later re-open creates a fresh
    event. Best-effort."""
    if not task.calendar_event_id:
        return
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    await _delete_anchored_commutes(
        session, calendar_id=calendar_id, anchor_id=task.calendar_event_id, task=task,
    )
    try:
        await delete_event(
            session, calendar_id=calendar_id, event_id=task.calendar_event_id,
        )
    except Exception as exc:  # noqa: BLE001 — never let calendar break task state changes
        log.warning("calendar delete failed · task=%s err=%s", task.id, exc)
        return

    tasks_store.clear_calendar_event(session, task)
    session.commit()


# Legs sit within a few hours of their anchor; this window is generous enough
# to catch even a long transit leg on either side of the task slot.
_LEG_SWEEP_MARGIN = timedelta(hours=6)


async def _delete_anchored_commutes(
    session: Session,
    *,
    calendar_id: str,
    anchor_id: str,
    task,
) -> None:
    if not get_settings().commute_enabled or task.scheduled_date is None:
        return
    try:
        events = await list_events_between(
            session,
            calendar_ids=[calendar_id],
            time_min=task.scheduled_date - _LEG_SWEEP_MARGIN,
            time_max=task.scheduled_date + _LEG_SWEEP_MARGIN,
        )
        for ev in events:
            key = commute_leg_key(ev)
            if key is None or anchor_id not in key:
                continue
            await delete_event(session, calendar_id=calendar_id, event_id=ev.id)
    except Exception as exc:  # noqa: BLE001 — leg cleanup must not block task state changes
        log.warning("commute leg cleanup failed · task=%s err=%s", task.id, exc)


def popup_reminders(minutes: int) -> dict:
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": minutes}]}


# Explicitly no reminder — distinct from omitting the field, which would let
# the calendar's default reminders fire.
SILENT_REMINDERS: dict = {"useDefault": False, "overrides": []}


def event_reminders(event: CalendarEvent) -> dict:
    """The requesting user's reminder config as Google returned it."""
    return event.raw.get("reminders") or {"useDefault": True}


def reminders_differ(event: CalendarEvent, desired: dict) -> bool:
    def _norm(r: dict) -> tuple:
        overrides = tuple(sorted(
            (str(o.get("method", "popup")), int(o.get("minutes", 0)))
            for o in r.get("overrides") or []
        ))
        return bool(r.get("useDefault")), overrides

    return _norm(event_reminders(event)) != _norm(desired)


def _task_description(task) -> str | None:
    parts: list[str] = []
    if task.description:
        parts.append(task.description)
    if task.link:
        parts.append(task.link)
    return "\n\n".join(parts) or None


def task_private_properties(task) -> dict[str, str]:
    return {
        PROP_MANAGED_BY: MANAGED_BY,
        PROP_KIND: KIND_TASK,
        "task_id": str(task.id),
    }


def commute_private_properties(*, origin_anchor: str, dest_anchor: str) -> dict[str, str]:
    return {
        PROP_MANAGED_BY: MANAGED_BY,
        PROP_KIND: KIND_COMMUTE,
        "origin_anchor": origin_anchor,
        "dest_anchor": dest_anchor,
    }


def commute_leg_key(event: CalendarEvent) -> tuple[str, str] | None:
    """`(origin_anchor, dest_anchor)` identity of a commute event, or None
    for non-commutes and legacy-format commutes (which callers treat as
    stale and delete on the next replan)."""
    if not is_commute_event(event):
        return None
    props = private_properties(event)
    origin = props.get("origin_anchor")
    dest = props.get("dest_anchor")
    return (origin, dest) if origin and dest else None


def private_properties(event: CalendarEvent) -> dict[str, str]:
    return event.private_properties


def is_managed_event(event: CalendarEvent) -> bool:
    return private_properties(event).get(PROP_MANAGED_BY) == MANAGED_BY


def is_task_event(event: CalendarEvent) -> bool:
    props = private_properties(event)
    return props.get(PROP_MANAGED_BY) == MANAGED_BY and props.get(PROP_KIND) == KIND_TASK


def is_commute_event(event: CalendarEvent) -> bool:
    props = private_properties(event)
    return props.get(PROP_MANAGED_BY) == MANAGED_BY and props.get(PROP_KIND) == KIND_COMMUTE


def is_free_event(event: CalendarEvent) -> bool:
    """Google's show-as-Free flag. All-day events default to free; timed
    events default to busy."""
    return event.raw.get("transparency") == "transparent"


def _clean_private_properties(props: dict[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in props.items()
        if key is not None and value is not None
    }
