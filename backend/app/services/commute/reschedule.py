"""Move task events that overlap freshly derived commute legs."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.task import Task
from app.services.commute.legs import PlannedLeg
from app.services.plan.schedule import Interval, schedule_task

log = logging.getLogger(__name__)

# Above this many simultaneously-moved tasks we collapse the per-task
# "Rescheduled" notifications into one summary.
BATCH_NOTIFY_THRESHOLD = 2


async def reschedule_overlapping_tasks(
    session: Session,
    legs: list[PlannedLeg],
    *,
    account_key: str | None = None,
    _depth: int = 0,
) -> int:
    if not legs:
        return 0

    stmt = (
        select(Task)
        .where(Task.calendar_event_id.is_not(None))
        .where(Task.due_date.is_not(None))
        .order_by(Task.due_date.asc())
    )
    tasks = list(session.execute(stmt).scalars())

    affected: list[Task] = []
    for task in tasks:
        current = await _current_interval(session, task)
        if current is None:
            continue
        if _first_overlap(current, legs, task.calendar_event_id) is None:
            continue
        affected.append(task)

    if not affected:
        return 0

    # Suppress per-task notifications when we're moving many at once — one
    # aggregate notification beats a notification storm. The per-task tag
    # stays available for the summary fallback path.
    batch = len(affected) > BATCH_NOTIFY_THRESHOLD
    moved_slots: list[tuple[Task, datetime, datetime]] = []
    for task in affected:
        current = await _current_interval(session, task)
        if current is None:
            continue
        overlap = _first_overlap(current, legs, task.calendar_event_id)
        if overlap is None:
            continue
        result = await schedule_task(
            session,
            task,
            block=overlap,
            account_key=account_key,
            notify=not batch,
            _depth=_depth,
        )
        if result is not None:
            moved_slots.append((task, result[0], result[1]))

    return len(moved_slots)


async def _current_interval(session: Session, task: Task) -> Interval | None:
    if not task.calendar_event_id:
        return None
    from app.config import get_settings
    from app.services.calendar import get_event

    calendar_id = (get_settings().google_calendar_id or "").strip()
    if not calendar_id:
        return None
    try:
        event = await get_event(session, calendar_id=calendar_id, event_id=task.calendar_event_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("commute.reschedule · current event lookup failed task=%s err=%s", task.id, exc)
        return None
    return Interval(event.start, event.end, event.id)


def _first_overlap(
    task_interval: Interval,
    legs: list[PlannedLeg],
    task_event_id: str,
) -> Interval | None:
    for leg in legs:
        # A task's own legs touch its slot by construction — not a conflict.
        if task_event_id in leg.key:
            continue
        if leg.depart < task_interval.end and task_interval.start < leg.arrive:
            return Interval(leg.depart, leg.arrive, leg.dest_anchor)
    return None
