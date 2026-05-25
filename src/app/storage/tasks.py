from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.task import Task
from app.schemas.task import TaskCreate


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


def update(
    session: Session,
    task_id: uuid.UUID,
    *,
    title: str | None = None,
    description: str | None = None,
    link: str | None = None,
    due_date: datetime | None = None,
    estimation: int | None = None,
    location: str | None = None,
    label: str | None = None,
    ai_doable: str | None = None,
) -> Task | None:
    row = session.get(Task, task_id)
    if row is None:
        return None
    if title is not None:
        row.title = title
    if description is not None:
        row.description = description
    if link is not None:
        row.link = link
    if due_date is not None:
        row.due_date = due_date
    if estimation is not None:
        row.estimation = estimation
    if location is not None:
        row.location = location
    if label is not None:
        row.label = label
    if ai_doable is not None:
        row.ai_doable = ai_doable
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
