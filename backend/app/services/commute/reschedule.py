"""Move task events that overlap freshly derived commute legs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.clients import tasks as tasks_store
from app.db.models.task import Task
from app.services.commute.legs import PlannedLeg
from app.services.plan.schedule import Interval, schedule_task

log = logging.getLogger(__name__)

# Above this many simultaneously-moved tasks we collapse the per-task
# "Rescheduled" notifications into one summary.
BATCH_NOTIFY_THRESHOLD = 2

# Slack around the legs' span when pre-filtering candidate tasks by their
# stored slot — covers an event the user dragged since the last discover sync.
_CANDIDATE_MARGIN = timedelta(days=1)


async def reschedule_overlapping_tasks(
    session: Session,
    legs: list[PlannedLeg],
    *,
    account_key: str | None = None,
    _depth: int = 0,
) -> int:
    if not legs:
        return 0

    # Only *open* tasks whose stored slot sits near the replanned legs can
    # overlap them — probing every task ever mirrored would fetch (and 404 on)
    # long-closed tasks with stale event links, every single pass.
    span_start = min(leg.depart for leg in legs) - _CANDIDATE_MARGIN
    span_end = max(leg.arrive for leg in legs) + _CANDIDATE_MARGIN
    tasks = [
        task
        for task in tasks_store.open_scheduled_between(
            session, time_min=span_start, time_max=span_end
        )
        if task.calendar_event_id and task.due_date
    ]
    tasks.sort(key=lambda task: task.due_date)

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
        if _is_gone(exc):
            # The mirrored event was deleted out from under us — drop the
            # stale link so the task stops 404ing on every pass and gets a
            # fresh event on its next (re)schedule.
            log.info(
                "commute.reschedule · event gone; clearing stale link task=%s event=%s",
                task.id, task.calendar_event_id,
            )
            task.calendar_event_id = None
            session.commit()
        else:
            log.warning(
                "commute.reschedule · current event lookup failed task=%s err=%s", task.id, exc,
            )
        return None
    return Interval(event.start, event.end, event.id)


def _is_gone(exc: Exception) -> bool:
    import httpx

    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (404, 410)


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
