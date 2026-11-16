"""Agent hand-offs on tasks."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.handoff import TaskHandoff

ACTIVE_STATES = ("proposed", "running")


def create(session: Session, *, task_id: uuid.UUID, kind: str, brief: str, state: str) -> TaskHandoff:
    row = TaskHandoff(task_id=task_id, kind=kind, brief=brief, state=state)
    session.add(row)
    session.flush()
    return row


def get(session: Session, handoff_id: uuid.UUID) -> TaskHandoff | None:
    return session.get(TaskHandoff, handoff_id)


def list_for_task(session: Session, task_id: uuid.UUID) -> list[TaskHandoff]:
    """Newest first."""
    return list(session.scalars(
        select(TaskHandoff)
        .where(TaskHandoff.task_id == task_id)
        .order_by(TaskHandoff.created_at.desc())
    ))


def latest_for_task(session: Session, task_id: uuid.UUID) -> TaskHandoff | None:
    return session.scalars(
        select(TaskHandoff)
        .where(TaskHandoff.task_id == task_id)
        .order_by(TaskHandoff.created_at.desc())
        .limit(1)
    ).first()


def active_for_task(session: Session, task_id: uuid.UUID) -> TaskHandoff | None:
    return session.scalars(
        select(TaskHandoff)
        .where(TaskHandoff.task_id == task_id, TaskHandoff.state.in_(ACTIVE_STATES))
        .limit(1)
    ).first()


def finish(
    session: Session, row: TaskHandoff, state: str, result: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
) -> None:
    row.state = state
    if result is not None:
        row.result = result
    if trace is not None:
        row.trace = trace
    session.flush()
