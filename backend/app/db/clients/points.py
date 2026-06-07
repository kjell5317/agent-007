from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.points_entry import PointsEntry


def total(session: Session) -> float:
    """Current points total — the sum of every ledger entry's amount."""
    stmt = select(func.coalesce(func.sum(PointsEntry.amount), 0.0))
    return float(session.execute(stmt).scalar_one() or 0.0)


def has_task_entry(session: Session, task_id: uuid.UUID) -> bool:
    """Whether this task has already been awarded points (idempotency guard)."""
    stmt = (
        select(func.count())
        .select_from(PointsEntry)
        .where(PointsEntry.source == "task", PointsEntry.task_id == task_id)
    )
    return int(session.execute(stmt).scalar_one() or 0) > 0


def add_entry(
    session: Session,
    *,
    source: str,
    factor: float,
    quantity: float,
    amount: float,
    section: str | None = None,
    action_name: str | None = None,
    task_id: uuid.UUID | None = None,
) -> PointsEntry:
    entry = PointsEntry(
        source=source,
        factor=factor,
        quantity=quantity,
        amount=amount,
        section=section,
        action_name=action_name,
        task_id=task_id,
    )
    session.add(entry)
    session.commit()
    return entry
