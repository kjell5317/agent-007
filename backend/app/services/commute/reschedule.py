"""Move task events that overlap freshly planned commute windows."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.task import Task
from app.services.commute.planner import CommutePlan
from app.services.plan.schedule import Interval, schedule

log = logging.getLogger(__name__)


async def reschedule_overlapping_tasks(
    session: Session,
    plans: list[CommutePlan],
    *,
    account_key: str | None = None,
) -> int:
    if not plans:
        return 0

    intervals = [Interval(plan.depart, plan.arrive, plan.related_event_id) for plan in plans]
    stmt = (
        select(Task)
        .where(Task.calendar_event_id.is_not(None))
        .where(Task.due_date.is_not(None))
        .order_by(Task.due_date.asc())
    )
    tasks = list(session.execute(stmt).scalars())

    moved = 0
    for task in tasks:
        current = await _current_interval(session, task)
        if current is None:
            continue
        overlap = _first_overlap(current, intervals)
        if overlap is None:
            continue
        await schedule(session, task, block=overlap, account_key=account_key)
        moved += 1
    return moved


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


def _first_overlap(task_interval: Interval, intervals: list[Interval]) -> Interval | None:
    for interval in intervals:
        if interval.start < task_interval.end and task_interval.start < interval.end:
            return interval
    return None
