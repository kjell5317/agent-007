from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from app.db.models.raw_input import RawInput
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate

# When the agent can't extract a deadline, give the task one far enough out
# that the planner still has a window to place it (matches the planner's
# LEAD_DAYS look-ahead).
DEFAULT_DUE_HORIZON = timedelta(days=7)


def count_since(session: Session, ts: datetime) -> int:
    """Count tasks created after `ts` — drives the Tasks-tab unread badge.

    Excludes manual tasks (every linked raw_input has source='manual'): the
    user just created those via POST /tasks, no need to badge themselves.
    """
    stmt = (
        select(func.count(func.distinct(Task.id)))
        .join(RawInput, RawInput.task_id == Task.id)
        .where(Task.created_at > ts, RawInput.source != "manual")
    )
    return int(session.execute(stmt).scalar_one() or 0)


def is_manual_for(
    session: Session, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, bool]:
    """Map each task to whether every one of its raw_inputs is `manual`."""
    if not task_ids:
        return {}
    rows = session.execute(
        select(RawInput.task_id, RawInput.source).where(
            RawInput.task_id.in_(task_ids)
        )
    ).all()
    by_task: dict[uuid.UUID, list[str]] = {}
    for r in rows:
        by_task.setdefault(r.task_id, []).append(r.source)
    return {
        tid: all(s == "manual" for s in sources)
        for tid, sources in by_task.items()
    }


def create(session: Session, payload: TaskCreate) -> Task:
    due_date = payload.due_date or datetime.now(timezone.utc) + DEFAULT_DUE_HORIZON
    row = Task(
        title=payload.title,
        description=payload.description,
        link=payload.link,
        due_date=due_date,
        estimation=payload.estimation,
        location=payload.location,
        label=payload.label,
    )
    session.add(row)
    session.flush()
    return row


def get(session: Session, task_id: uuid.UUID) -> Task | None:
    return session.get(Task, task_id)


def set_schedule(
    session: Session,
    task: Task,
    *,
    event_id: str | None,
    scheduled_date: datetime | None,
) -> Task:
    task.calendar_event_id = event_id
    task.scheduled_date = scheduled_date
    session.flush()
    return task


def clear_calendar_event(session: Session, task: Task) -> Task:
    """Drop the calendar mirror pointer, keeping the task's slot.

    `scheduled_date` is NOT NULL — a deleted mirror doesn't un-schedule the
    task, it just detaches the (now-gone) event so a later re-plan creates a
    fresh one."""
    task.calendar_event_id = None
    session.flush()
    return task


_UPDATABLE = {
    "title", "description", "link", "due_date", "estimation",
    "location", "label",
}


def update(session: Session, task_id: uuid.UUID, **fields) -> Task | None:
    """Apply the given fields to the task. A field's *presence* in `fields`
    means "change it" — even when the value is None (which clears it).
    Callers should pass `model_dump(exclude_unset=True)` to express "leave
    untouched" for the fields that should stay as they are.

    `title` is non-nullable in the schema; passing `title=None` is rejected
    so we don't crash on flush. The other six are nullable."""
    row = session.get(Task, task_id)
    if row is None:
        return None
    for key, value in fields.items():
        if key not in _UPDATABLE:
            continue
        if key == "title" and value is None:
            continue
        setattr(row, key, value)
    session.flush()
    return row


# Task status is derived: the status of the most-recent *anchor* raw_input
# pointing at the task is the task's current state. Rows with status='duplicate'
# are references back to an existing task, not state transitions for it, so
# they are excluded — otherwise a new duplicate would visually flip the
# original task's status to 'duplicate'. `received_at DESC` is indexed.
_LATEST_STATUS_SQL = text(
    """
    SELECT DISTINCT ON (task_id) task_id, status
    FROM raw_inputs
    WHERE task_id = ANY(:ids)
      AND status <> 'duplicate'
    ORDER BY task_id, received_at DESC
    """
)


def latest_status_for(
    session: Session, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not task_ids:
        return {}
    rows = session.execute(_LATEST_STATUS_SQL, {"ids": task_ids}).all()
    return {r.task_id: r.status for r in rows}


def list_(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[tuple[Task, str]]:
    """Return tasks with their derived status.

    If `status` is given, filter by the latest-raw_input status. The filter
    runs in SQL (not after `limit`) so `limit` bounds the matching rows — else
    an early row could evict every open task out of the window.
    """
    display_date = func.coalesce(Task.scheduled_date, Task.due_date)
    stmt = select(Task)
    if status is not None:
        latest = (
            select(
                RawInput.task_id.label("task_id"),
                RawInput.status.label("status"),
                func.row_number()
                .over(
                    partition_by=RawInput.task_id,
                    order_by=RawInput.received_at.desc(),
                )
                .label("rn"),
            )
            .where(RawInput.status != "duplicate")
            .subquery()
        )
        stmt = stmt.outerjoin(
            latest, and_(latest.c.task_id == Task.id, latest.c.rn == 1)
        ).where(func.coalesce(latest.c.status, "open") == status)
    stmt = stmt.order_by(
        display_date.is_(None),
        display_date.asc(),
        Task.created_at.desc(),
    ).limit(limit)
    rows = list(session.execute(stmt).scalars())
    statuses = latest_status_for(session, [r.id for r in rows])
    return [(r, statuses.get(r.id, "open")) for r in rows]


def overdue_scheduled_open(
    session: Session,
    *,
    cutoff: datetime,
    limit: int = 100,
) -> list[Task]:
    # The status filter MUST run in SQL, before `limit`. Otherwise closed tasks
    # — of which the scheduled_date backfill created a large pile all tied at
    # one instant — fill the limit window and evict the open tasks we actually
    # need to reschedule (same trap as `list_`).
    latest = (
        select(
            RawInput.task_id.label("task_id"),
            RawInput.status.label("status"),
            func.row_number()
            .over(
                partition_by=RawInput.task_id,
                order_by=RawInput.received_at.desc(),
            )
            .label("rn"),
        )
        .where(RawInput.status != "duplicate")
        .subquery()
    )
    stmt = (
        select(Task)
        .outerjoin(latest, and_(latest.c.task_id == Task.id, latest.c.rn == 1))
        .where(Task.scheduled_date <= cutoff)
        .where(func.coalesce(latest.c.status, "open") == "open")
        .order_by(Task.scheduled_date.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def open_scheduled_between(
    session: Session,
    *,
    time_min: datetime,
    time_max: datetime,
    exclude_task_id: uuid.UUID | None = None,
) -> list[Task]:
    """Open tasks whose stored `scheduled_date` starts in `[time_min, time_max)`.

    The planner uses this as a backstop to its live Google Calendar read: a
    task scheduled moments earlier may not appear in `events.list` yet (read-
    after-write lag), so its DB row is the authoritative record of the slot.
    Only genuinely open tasks count — same status filter as
    `overdue_scheduled_open`, so completed tasks never block a fresh placement.
    """
    latest = (
        select(
            RawInput.task_id.label("task_id"),
            RawInput.status.label("status"),
            func.row_number()
            .over(
                partition_by=RawInput.task_id,
                order_by=RawInput.received_at.desc(),
            )
            .label("rn"),
        )
        .where(RawInput.status != "duplicate")
        .subquery()
    )
    stmt = (
        select(Task)
        .outerjoin(latest, and_(latest.c.task_id == Task.id, latest.c.rn == 1))
        .where(Task.scheduled_date >= time_min)
        .where(Task.scheduled_date < time_max)
        .where(func.coalesce(latest.c.status, "open") == "open")
    )
    if exclude_task_id is not None:
        stmt = stmt.where(Task.id != exclude_task_id)
    return list(session.execute(stmt).scalars())
