"""Task ↔ Calendar mirror.

The two fire-and-forget hooks the rest of the app calls when a task changes:
`add_task_to_calendar` (on creation) and `update_task_in_calendar` (on edit).
Both swallow errors — a calendar failure must never break the underlying
task operation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.labels import color_for
from app.services.google_calendar.events import create_event, delete_event, patch_event

log = logging.getLogger(__name__)


async def add_task_to_calendar(session: Session, task) -> None:
    """Fire-and-forget: mirror `task` as a Google Calendar event.

    The event ends at `task.due_date` and starts `estimation` minutes earlier
    (falling back to `google_calendar_default_event_minutes` when estimation
    is missing). Skipped silently when due_date is None, when no Google
    account is connected, or when google_calendar_id is empty. Never raises
    — calendar failures must not break task creation. On success the new
    event id is persisted on `task.calendar_event_id` and committed.
    """
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    if task.due_date is None:
        log.debug("calendar sync · task=%s skipped (no due_date)", task.id)
        return

    start, end = _task_window(task, settings)
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
        log.warning("calendar sync failed · task=%s err=%s", task.id, exc)
        return

    task.calendar_event_id = event.id
    session.commit()


async def update_task_in_calendar(session: Session, task) -> None:
    """Fire-and-forget: push the task's current fields to its calendar event.

    If the task has no `calendar_event_id` yet (e.g. it was created without a
    due_date and now has one), delegates to `add_task_to_calendar` so the
    event is created. Never raises.
    """
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    if task.due_date is None:
        log.debug("calendar sync · task=%s skipped (no due_date)", task.id)
        return
    if not task.calendar_event_id:
        await add_task_to_calendar(session, task)
        return

    start, end = _task_window(task, settings)
    # Google reads "" as "clear colorId back to the calendar default", which
    # is what we want when the label was removed or no longer maps to a color.
    color_id = color_for(task.label) or ""
    try:
        await patch_event(
            session,
            calendar_id=calendar_id,
            event_id=task.calendar_event_id,
            summary=task.title,
            start=start,
            end=end,
            description=_task_description(task) or "",
            location=task.location or "",
            color_id=color_id,
        )
    except Exception as exc:  # noqa: BLE001 — never let calendar break task updates
        log.warning("calendar update failed · task=%s err=%s", task.id, exc)


async def remove_task_from_calendar(session: Session, task) -> None:
    """Fire-and-forget: drop the task's calendar event (close / not_task path).

    No-op when the task has no mirrored event yet, or when no calendar is
    configured. Clears `task.calendar_event_id` on success so a later reopen
    creates a fresh event.
    """
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


def _task_window(task, settings) -> tuple[datetime, datetime]:
    end = task.due_date
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    minutes = task.estimation or settings.google_calendar_default_event_minutes
    return end - timedelta(minutes=minutes), end


def _task_description(task) -> str | None:
    parts: list[str] = []
    if task.description:
        parts.append(task.description)
    if task.link:
        parts.append(task.link)
    return "\n\n".join(parts) or None
