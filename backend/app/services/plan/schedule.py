"""Task placement entry points.

Every placement is a solve (`app.services.plan.replan`): `schedule_task` asks
for one on behalf of a task, `schedule_tasks` for several at once, and both
send the per-task notifications the solve leaves to its callers.
`reschedule_event` is the calendar-discover dispatcher for an edit that
collides with one of our events.

`_schedule_lock` serializes every solve process-wide: a solve reads the live
calendar, so two at once would each place into a snapshot missing the
other's writes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import tasks as tasks_store
from app.db.models.task import Task
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

_schedule_lock = asyncio.Lock()


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    event_id: str | None = None


async def schedule_task(
    session: Session,
    task: Task,
    *,
    block: Interval | None = None,
    pull_in: bool = False,
    account_key: str | None = None,
    notify: bool = True,
    primary_action: dict[str, str] | None = None,
) -> tuple[datetime, datetime] | None:
    """Place `task` with a solve, which also writes its calendar mirror (and
    the commute legs).

    `block` keeps the task out of that span for this solve (the slot it is
    leaving). `pull_in=True` plans a task due past the horizon this week
    instead of leaving it for later, and keeps the slot it gets like a hand
    placement.

    `notify=False` suppresses the per-task scheduled/deferred notification.
    `primary_action` is forwarded opaquely to the "Scheduled" notification to
    swap its leading button (kotx tasks use this).

    Returns the placed `(start, end)`, or None — also inside a `replan.batch`,
    whose one solve on exit places every collected task.
    """
    if getattr(task, "is_container", False):
        return None
    if tasks_store.latest_status_for(session, [task.id]).get(task.id) == "closed":
        return None
    if task.due_date is None:
        log.debug("plan.schedule · task=%s has no due_date", task.id)
        return None

    options = {"block": block, "pull_in": pull_in, "account_key": account_key,
               "notify": notify, "primary_action": primary_action}
    from app.services.plan.replan import batched

    pending = batched()
    if pending is not None:
        pending.append((task, options))
        return None
    return (await schedule_tasks(session, [(task, options)])).get(task.id)


async def schedule_tasks(
    session: Session, requests: list[tuple[Task, dict]]
) -> dict:
    """One global solve covering every requested task, then the per-task
    notifications `schedule_task` promises. Returns
    `{task_id: (start, end) | None}`."""
    from app.services.plan.replan import request_replan

    fresh = {task.id: task.calendar_event_id is None for task, _ in requests}
    blocks: dict = {}
    account_key = None
    for task, options in requests:
        block = options.get("block")
        if block is not None:
            blocks.setdefault(task.id, []).append((block.start, block.end))
        account_key = account_key or options.get("account_key")
    pulled_in = {task.id for task, options in requests if options.get("pull_in")}
    session.commit()
    try:
        result = await request_replan(
            session, reason="schedule_task", blocks=blocks, pulled_in=pulled_in,
            account_key=account_key,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("plan.schedule · solve failed err=%s", exc, exc_info=True)
        session.rollback()
        result = None

    from app.services.notify import clear_task_notification, notify_task_deferred

    out: dict = {}
    for task, options in requests:
        session.refresh(task)
        interval = scheduled_interval_for(task) if task.schedule_state == "placed" else None
        out[task.id] = (interval.start, interval.end) if interval is not None else None
        if result is None or not options.get("notify", True):
            continue
        if interval is not None and task.calendar_event_id:
            if fresh[task.id]:
                await announce_placement(
                    session, task, start=interval.start, end=interval.end,
                    primary_action=options.get("primary_action"),
                )
            else:
                await clear_task_notification(task.id)
        elif (
            task.schedule_state == "deferred"
            and fresh[task.id]
            and not tasks_store.created_manually(session, task)
        ):
            await notify_task_deferred(task, primary_action=options.get("primary_action"))
    return out


async def announce_placement(
    session: Session,
    task: Task,
    *,
    start: datetime,
    end: datetime,
    primary_action: dict[str, str] | None = None,
) -> None:
    """The "Scheduled" push for a task's first slot. A task the user typed in
    is already known to them, so only a slot past its deadline is news; an
    on-time slot just clears a lingering "could not schedule" warning."""
    from app.services.notify import (
        clear_task_notification,
        notify_deadline_missed,
        notify_task_created,
    )

    if not tasks_store.created_manually(session, task):
        await notify_task_created(task, start=start, end=end, primary_action=primary_action)
        return
    due = to_user_tz(task.due_date) if task.due_date else None
    if due is None or end <= due:
        await clear_task_notification(task.id)
    elif due <= datetime.now(user_tz()):
        # The solver only warns about deadlines still ahead (`newly_late`);
        # one already behind is announced here instead.
        await notify_deadline_missed(task, start=start, end=end)


async def drop_if_closed(session: Session, task: Task) -> bool:
    """Undo a just-written placement when the task was closed meanwhile.

    A close that committed before the event existed found nothing to delete,
    and a status check made before the write can't see that close. Checking
    after the write closes the gap: of the two, whichever commits last sees
    the other."""
    if tasks_store.latest_status_for(session, [task.id]).get(task.id) != "closed":
        return False
    from app.services.calendar import delete_task_event

    log.info("plan.schedule · task=%s closed while being placed; dropping its event", task.id)
    await delete_task_event(session, task)
    return True


async def reschedule_event(
    session: Session,
    event_id: str,
    *,
    account_key: str | None = None,
) -> None:
    """Dispatch a calendar-discover conflict. A task event re-plans its task;
    the only other managed events are commute legs, which a fresh solve
    re-derives."""
    task = _task_for_event(session, event_id)
    if task is not None:
        result = await schedule_task(session, task, account_key=account_key)
        if result is not None:
            from app.events import publish_task

            publish_task(session, task.id)
        return
    from app.services.plan.commute import replan_commutes

    await replan_commutes(session, account_key=account_key)


def _task_for_event(session: Session, event_id: str) -> Task | None:
    stmt = select(Task).where(Task.calendar_event_id == event_id)
    return session.execute(stmt).scalar_one_or_none()


def scheduled_interval_for(task: Task) -> Interval | None:
    if task.scheduled_date is None:
        return None
    settings = get_settings()
    start = to_user_tz(task.scheduled_date)
    end = start + timedelta(minutes=_duration_minutes(task, settings))
    return Interval(start, end, task.calendar_event_id)


def _duration_minutes(task: Task, settings) -> int:
    """Booked length of the task's slot: what the solver wrote (the corrected
    estimate) when set, else the estimate."""
    booked = getattr(task, "booked_minutes", None)
    if booked:
        return booked
    raw = task.estimation or settings.google_calendar_default_event_minutes
    return max(5, int(raw or 30))


def estimate_minutes(task: Task, settings) -> int:
    raw = task.estimation or settings.google_calendar_default_event_minutes
    return max(5, int(raw or 30))
