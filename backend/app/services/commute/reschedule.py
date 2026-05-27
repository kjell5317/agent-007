"""Reschedule tasks whose calendar events overlap a planned commute.

Tasks are mirrored to the calendar as events; if a freshly-planned commute
collides with one, we move the task. Re-uses
[plan_task_slot][app.services.plan.schedule.plan_task_slot] with the commute
windows passed in as `extra_busy`, so it just picks the next free slot under
the existing 10-20 / 08-24 daily-window rules.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.task import Task
from app.services.commute.planner import CommutePlan
from app.services.plan.schedule import Interval, notify_no_slot, plan_task_slot
from app.services.calendar import update_task_event
from app.services.calendar.client import authorized_client, normalize

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
    event_intervals = await _task_event_intervals(
        session,
        calendar_id=calendar_id,
        tasks=tasks,
        commute_intervals=commute_intervals,
        account_key=account_key,
    )

    moved = 0
    for task in tasks:
        overlapping = _overlap(event_intervals.get(task.calendar_event_id), commute_intervals)
        if overlapping is None:
            continue
        try:
            new_start, new_end = await plan_task_slot(
                session,
                task,
                extra_busy=commute_intervals,
                account_key=account_key,
            )
        except ValueError as exc:
            log.warning(
                "commute · no slot while rescheduling task=%s err=%s",
                task.id, exc,
            )
            await notify_no_slot(task)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · reschedule planning failed task=%s err=%s",
                task.id, exc,
            )
            continue
        try:
            await update_task_event(
                session,
                task,
                start=new_start,
                end=new_end,
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


async def _task_event_intervals(
    session: Session,
    *,
    calendar_id: str,
    tasks: list[Task],
    commute_intervals: list[Interval],
    account_key: str | None,
) -> dict[str, Interval]:
    event_ids = {task.calendar_event_id for task in tasks if task.calendar_event_id}
    if not event_ids or not commute_intervals:
        return {}

    time_min = min(itv.start for itv in commute_intervals)
    time_max = max(itv.end for itv in commute_intervals)
    client = await authorized_client(session, account_key)
    items = await client.list_events(calendar_id, time_min=time_min, time_max=time_max)
    out: dict[str, Interval] = {}
    for item in items:
        if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
            continue
        ev = normalize(item, calendar_id)
        if ev.id in event_ids and not ev.all_day:
            out[ev.id] = Interval(ev.start.astimezone(), ev.end.astimezone())
    return out


def _overlap(task_interval: Interval | None, intervals: list[Interval]) -> Interval | None:
    """Return the first commute interval that overlaps the task event."""
    if task_interval is None:
        return None
    for itv in intervals:
        if itv.start < task_interval.end and task_interval.start < itv.end:
            return itv
    return None
