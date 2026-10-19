"""User corrections of agent output — the labelled examples for the agent ML
hints and the duration correction."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.scheduling import TaskOverride

FIELDS = frozenset(
    {"due_date", "deadline_kind", "estimation", "label", "work_type", "effort", "is_task"}
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def add(
    session: Session,
    *,
    field: str,
    agent_value: Any,
    user_value: Any,
    source: str,
    task_id: uuid.UUID | None = None,
    raw_input_id: uuid.UUID | None = None,
) -> TaskOverride:
    if field not in FIELDS:
        raise ValueError(f"unknown override field: {field}")
    row = TaskOverride(
        task_id=task_id,
        raw_input_id=raw_input_id,
        field=field,
        agent_value=_jsonable(agent_value),
        user_value=_jsonable(user_value),
        source=source,
    )
    session.add(row)
    session.flush()
    return row


def record_changes(
    session: Session,
    *,
    task_id: uuid.UUID,
    before: dict[str, Any],
    after: dict[str, Any],
    source: str,
    raw_input_id: uuid.UUID | None = None,
) -> int:
    """One row per tracked field whose value actually changed."""
    written = 0
    for field in FIELDS & before.keys() & after.keys():
        if _jsonable(before[field]) == _jsonable(after[field]):
            continue
        add(
            session,
            field=field,
            agent_value=before[field],
            user_value=after[field],
            source=source,
            task_id=task_id,
            raw_input_id=raw_input_id,
        )
        written += 1
    return written


def list_for_field(session: Session, field: str) -> list[TaskOverride]:
    return list(session.scalars(
        select(TaskOverride).where(TaskOverride.field == field).order_by(TaskOverride.created_at)
    ))


def exists_for_source(session: Session, task_id: uuid.UUID, source: str) -> bool:
    return session.scalar(
        select(TaskOverride.id)
        .where(TaskOverride.task_id == task_id, TaskOverride.source == source)
        .limit(1)
    ) is not None


USER_SOURCES = ("ui", "calendar", "notification")


def task_ids_with_user_override(session: Session, field: str) -> set[uuid.UUID]:
    return set(session.scalars(
        select(TaskOverride.task_id)
        .where(TaskOverride.field == field, TaskOverride.source.in_(USER_SOURCES))
        .distinct()
    ))
