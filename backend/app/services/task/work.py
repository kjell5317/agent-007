"""Work sessions: Start, Pause (break / waiting / interrupted), Complete, and
work logged afterwards.

Starting pins the task's slot to now while the session runs and pauses any
other running task ("switched"). A pause hands what's left of the estimate
back to the planner — after a short block for a break, a longer one for an
interruption — except "waiting", which takes the task off the calendar until
it is resumed. Break time is never task time: finished sessions add their
own minutes to `worked_minutes`, and the planner books only the remainder.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import schedule_log as log_store
from app.db.clients import tasks as tasks_store
from app.db.clients import work as work_store
from app.db.models.raw_input import RawInput
from app.db.models.task import Task
from app.db.models.work import WorkSession
from app.db.schemas.task import TaskCreate
from app.events import publish_input, publish_task
from app.services.notifications.notify import clear_task_notifications
from app.services.plan import Interval, pin_running, remaining_minutes, schedule_task
from app.services.plan.cue import booked_slot
from app.services.task.close import close_task

log = logging.getLogger(__name__)

PAUSE_REASONS = ("break", "waiting", "interrupted")
# How long the remainder stays off the calendar after a pause.
PAUSE_BLOCK = {
    "break": timedelta(minutes=10),
    "interrupted": timedelta(minutes=30),
    "switched": timedelta(0),
    "abandoned": timedelta(0),
}
MAX_LOGGED = timedelta(hours=16)
# `estimation_source` of a task created from logged work.
LOGGED = "logged"
# A logged span may end a little ahead of the server clock (phone clock drift).
CLOCK_SLACK = timedelta(minutes=5)


def _open(session: Session, task: Task) -> None:
    if tasks_store.latest_status_for(session, [task.id]).get(task.id, "open") != "open":
        raise ValueError("Task isn't open")
    if task.is_container:
        raise ValueError("A parent task is worked on through its subtasks")


def validate_span(started_at: datetime, ended_at: datetime, now: datetime) -> None:
    if ended_at <= started_at:
        raise ValueError("The end must be after the start")
    if ended_at > now + CLOCK_SLACK:
        raise ValueError("Work can't end in the future")
    if ended_at - started_at > MAX_LOGGED:
        raise ValueError("A logged span can't be longer than 16 hours")


def _end(session: Session, task: Task, row: WorkSession, at: datetime, reason: str) -> None:
    work_store.end(session, row, at, reason)
    if reason != "abandoned":
        task.worked_minutes = (task.worked_minutes or 0) + round(row.minutes)
    task.running_since = None
    session.commit()


# Strong references: the loop only keeps weak ones to running tasks.
_solves: set[asyncio.Task] = set()


def _solve_later(task_id: uuid.UUID | None = None, *, block: Interval | None = None) -> None:
    """A solve can take the solver's whole time limit, so a tap answers
    first. With a `task_id`, that task is re-planned (kept out of `block`);
    without, everything is."""

    async def run() -> None:
        from app.db import SessionLocal
        from app.services.plan.replan import request_replan

        try:
            with SessionLocal() as session:
                if task_id is None:
                    await request_replan(session, reason="work_session")
                    return
                task = tasks_store.get(session, task_id)
                if task is not None:
                    await schedule_task(session, task, block=block)
                    publish_task(session, task.id)
        except Exception:  # noqa: BLE001 — the next solve picks the task up
            log.exception("work · background solve failed task=%s", task_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    job = loop.create_task(run())
    _solves.add(job)
    job.add_done_callback(_solves.discard)


def _replan_rest(task: Task, now: datetime, reason: str) -> None:
    """Hand the remainder back to the planner, off the slot the session sat in."""
    slot = booked_slot(task)
    start = min(slot[0], now) if slot is not None else now
    _solve_later(task.id, block=Interval(start, now + PAUSE_BLOCK[reason]))


async def start_task(session: Session, task: Task, *, now: datetime | None = None) -> WorkSession:
    now = now or datetime.now(timezone.utc)
    _open(session, task)
    current = work_store.running(session)
    if current is not None and current.task_id == task.id:
        return current
    switched = None
    if current is not None:
        switched = tasks_store.get(session, current.task_id)
        _end(session, switched, current, now, "switched")
    slot = booked_slot(task) if task.schedule_state == "placed" else None
    log_store.add(
        session, task_id=task.id, kind="started", actor="user",
        slot_start=task.scheduled_date if slot else None, slot_end=slot[1] if slot else None,
    )
    row = work_store.start(session, task.id, now)
    task.running_since = now
    task.waiting_since = None
    await pin_running(session, task, start=now, minutes=remaining_minutes(task, get_settings()))
    await clear_task_notifications(session, task.id, kinds={"start_cue", "new_task"})
    publish_task(session, task.id)
    if switched is not None:
        publish_task(session, switched.id)
        # Its solve also moves whatever sat where the new session runs.
        _replan_rest(switched, now, "switched")
    else:
        _solve_later()
    _refit_soon()
    return row


async def pause_task(
    session: Session, task: Task, reason: str, *, now: datetime | None = None
) -> None:
    if reason not in PAUSE_REASONS:
        raise ValueError(f"Unknown pause reason: {reason}")
    now = now or datetime.now(timezone.utc)
    row = work_store.open_for_task(session, task.id)
    if row is None:
        raise LookupError("Task isn't running")
    _end(session, task, row, now, reason)
    if reason == "waiting":
        await _set_waiting(session, task, now)
    else:
        _replan_rest(task, now, reason)
    publish_task(session, task.id)
    _refit_soon()


def stop_running(session: Session, task: Task, *, reason: str, at: datetime | None = None) -> None:
    """End the task's open session, if any — it was closed or dismissed
    outside the work controls."""
    row = work_store.open_for_task(session, task.id)
    if row is not None:
        _end(session, task, row, at or datetime.now(timezone.utc), reason)


async def _set_waiting(session: Session, task: Task, now: datetime) -> None:
    from app.services.calendar import delete_task_event

    await delete_task_event(session, task)
    task.waiting_since = now
    task.schedule_state = "waiting"
    task.scheduled_date = None
    task.booked_minutes = None
    session.commit()
    # The freed time and the commute legs around it.
    _solve_later()


async def resume_task(session: Session, task: Task) -> None:
    """Waiting is over: the task goes back to the planner."""
    if task.schedule_state != "waiting":
        return
    task.waiting_since = None
    task.schedule_state = "unplaced"
    session.commit()
    publish_task(session, task.id)
    _solve_later(task.id)


async def complete_task(session: Session, task: Task, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    row = work_store.open_for_task(session, task.id)
    if row is not None:
        _end(session, task, row, now, "completed")
    await close_task(session, task.id)


async def end_abandoned(session: Session, task: Task, *, at: datetime) -> None:
    """A session that ran to twice its booked length was forgotten: it ends
    at that point, counts no minutes, and the task is re-planned."""
    row = work_store.open_for_task(session, task.id)
    slot = booked_slot(task)
    if row is not None:
        _end(session, task, row, at, "abandoned")
    else:
        task.running_since = None
        session.commit()
    log_store.add(
        session, task_id=task.id, kind="abandoned", actor="system",
        slot_start=task.scheduled_date, slot_end=at,
    )
    session.commit()
    now = datetime.now(timezone.utc)
    start = min(slot[0], now) if slot is not None else now
    await schedule_task(session, task, block=Interval(start, now))
    publish_task(session, task.id)


async def log_work(
    session: Session,
    task: Task,
    *,
    started_at: datetime,
    ended_at: datetime,
    complete: bool,
    now: datetime | None = None,
) -> WorkSession:
    """Work done without a live session — forgotten to start, or done before
    the task existed. Completing closes the task."""
    now = now or datetime.now(timezone.utc)
    validate_span(started_at, ended_at, now)
    _open(session, task)
    row = work_store.add_logged(
        session, task.id, started_at, ended_at, "completed" if complete else "logged"
    )
    task.worked_minutes = (task.worked_minutes or 0) + round(row.minutes)
    session.commit()
    if complete:
        await close_task(session, task.id)
    else:
        publish_task(session, task.id)
        _solve_later(task.id)
    _refit_soon()
    return row


async def log_spontaneous(
    session: Session,
    *,
    title: str,
    started_at: datetime,
    ended_at: datetime,
    label: str | None = None,
    work_type: str | None = None,
    effort: str | None = None,
    now: datetime | None = None,
) -> Task:
    """Done before it was ever a task: create it, log the span, close it."""
    now = now or datetime.now(timezone.utc)
    validate_span(started_at, ended_at, now)
    minutes = max(5, round((ended_at - started_at).total_seconds() / 60))
    task = tasks_store.create(session, TaskCreate(
        title=title.strip(),
        due_date=ended_at,
        estimation=minutes,
        # Not a prediction: the duration model must not learn from it.
        estimation_source=LOGGED,
        label=label,
        work_type=work_type,
        effort=effort,
        deadline_kind="soft",
    ))
    task.schedule_state = "unplaced"
    task.scheduled_date = None
    raw = RawInput(
        source="manual", content=task.title, source_metadata={"manual": True, "logged": True},
        task_id=task.id, status="open", processed_at=now,
    )
    session.add(raw)
    session.commit()
    publish_input(session, raw.id)
    work_store.add_logged(session, task.id, started_at, ended_at, "completed")
    task.worked_minutes = minutes
    session.commit()
    await close_task(session, task.id)
    _refit_soon()
    log.info("work · logged spontaneous task=%s minutes=%d", task.id, minutes)
    return task


def _refit_soon() -> None:
    from app.services.learn.jobs import refit_soon

    refit_soon()
