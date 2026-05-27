from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models.raw_input import RawInput
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate


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
    row = Task(
        title=payload.title,
        description=payload.description,
        link=payload.link,
        due_date=payload.due_date,
        estimation=payload.estimation,
        location=payload.location,
        label=payload.label,
        ai_doable=payload.ai_doable,
    )
    session.add(row)
    session.flush()
    return row


def get(session: Session, task_id: uuid.UUID) -> Task | None:
    return session.get(Task, task_id)


_UPDATABLE = {
    "title", "description", "link", "due_date", "estimation",
    "location", "label", "ai_doable",
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

    If `status` is given, filter by the latest-raw_input status.
    """
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    rows = list(session.execute(stmt).scalars())
    statuses = latest_status_for(session, [r.id for r in rows])
    out = [(r, statuses.get(r.id, "open")) for r in rows]
    if status is not None:
        out = [t for t in out if t[1] == status]
    return out
