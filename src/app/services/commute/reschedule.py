"""Reschedule tasks whose calendar events overlap a planned commute.

Tasks are mirrored to the calendar as events; if a freshly-planned commute
collides with one, we move the task. Re-uses
[plan_task_slot][app.services.event_planning.plan_task_slot] with the commute
windows passed in as `extra_busy`, so it just picks the next free slot under
the existing 10-20 / 08-24 daily-window rules.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.task import Task
from app.services.commute.planner import CommutePlan
from app.services.event_planning import Interval, plan_task_slot
from app.services.google_calendar import patch_event

log = logging.getLogger(__name__)


async def reschedule_overlapping_tasks(
    session: Session,
    plans: list[CommutePlan],
    *,
    account_key: str | None = None,
) -> int:
    """Move every task event that overlaps a `CommutePlan`. Returns the count
    moved."""
    if not plans:
        return 0

    commute_intervals = [
        Interval(p.depart.astimezone(), p.arrive.astimezone()) for p in plans
    ]

    # Pull every task with a mirrored calendar event and a due_date (the
    # planner can only place tasks that have one). Bias toward soonest due
    # so urgent rescheduling happens first.
    stmt = (
        select(Task)
        .where(Task.calendar_event_id.is_not(None))
        .where(Task.due_date.is_not(None))
        .order_by(Task.due_date.asc())
    )
    tasks = list(session.execute(stmt).scalars())

    from app.config import get_settings

    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return 0

    moved = 0
    for task in tasks:
        overlapping = _overlap(task, commute_intervals)
        if overlapping is None:
            continue
        try:
            new_start, new_end = await plan_task_slot(
                session, task, extra_busy=commute_intervals,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · reschedule planning failed task=%s err=%s",
                task.id, exc,
            )
            continue
        try:
            await patch_event(
                session,
                calendar_id=calendar_id,
                event_id=task.calendar_event_id,
                start=new_start,
                end=new_end,
                account_key=account_key,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · reschedule patch failed task=%s err=%s",
                task.id, exc,
            )
            continue

        log.info(
            "commute · rescheduled task=%s out of commute %s → %s",
            task.id, overlapping.start.isoformat(), overlapping.end.isoformat(),
        )
        moved += 1
    return moved


def _overlap(task: Task, intervals: list[Interval]) -> Interval | None:
    """Return the first commute interval that overlaps the task's planned
    window. We approximate the task window as `[due_date - estimation,
    due_date]`, matching what `event_planning` would have placed it at if no
    other conflict existed."""
    if task.due_date is None:
        return None
    from app.config import get_settings

    duration_minutes = task.estimation or get_settings().google_calendar_default_event_minutes
    from datetime import timedelta

    due_local = task.due_date.astimezone()
    task_start = due_local - timedelta(minutes=duration_minutes)
    for itv in intervals:
        if itv.start < due_local and task_start < itv.end:
            return itv
    return None
