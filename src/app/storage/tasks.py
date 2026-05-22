from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.task import Task
from app.schemas.task import TaskCreate


def create(session: Session, payload: TaskCreate) -> Task:
    row = Task(
        title=payload.title,
        description=payload.description,
        estimated_minutes=payload.estimated_minutes,
        confidence=payload.confidence,
        location=payload.location,
        due_at=payload.due_at,
        source_links=list(payload.source_links or []),
        raw_input_id=payload.raw_input_id,
    )
    session.add(row)
    session.flush()
    return row


def get(session: Session, task_id: uuid.UUID) -> Task | None:
    return session.get(Task, task_id)


def list_(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[Task]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    return list(session.execute(stmt).scalars())


def update(
    session: Session,
    task_id: uuid.UUID,
    *,
    title: str | None = None,
    description: str | None = None,
    estimated_minutes: int | None = None,
    location: str | None = None,
    due_at: datetime | None = None,
    source_links: list[str] | None = None,
    status: str | None = None,
) -> Task | None:
    row = session.get(Task, task_id)
    if row is None:
        return None
    if title is not None:
        row.title = title
    if description is not None:
        row.description = description
    if estimated_minutes is not None:
        row.estimated_minutes = estimated_minutes
    if location is not None:
        row.location = location
    if due_at is not None:
        row.due_at = due_at
    if source_links is not None:
        row.source_links = list(source_links)
    if status is not None:
        row.status = status
    session.flush()
    return row


def search_similar(session: Session, query: str, *, k: int = 5) -> list[Task]:
    """Find open tasks loosely matching `query`.

    Placeholder until tasks are embedded and pgvector cosine search is wired in.
    For now: case-insensitive substring on title or description, ranked by recency.
    """
    pattern = f"%{query.strip()}%"
    stmt = (
        select(Task)
        .where(Task.status == "open")
        .where(or_(Task.title.ilike(pattern), Task.description.ilike(pattern)))
        .order_by(Task.created_at.desc())
        .limit(k)
    )
    return list(session.execute(stmt).scalars())
