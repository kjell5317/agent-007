from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.points_entry import PointsEntry


def _whole(amount: float) -> int:
    """Points are whole numbers; round any computed amount (e.g. 0.1 × 45 min)
    to the nearest integer, ties away from zero. `str()` first to dodge binary
    float noise (so 4.5 rounds to 5, not 4)."""
    return int(Decimal(str(amount)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def total(session: Session) -> int:
    """Current points total — the sum of every ledger entry's amount."""
    stmt = select(func.coalesce(func.sum(PointsEntry.amount), 0))
    return int(session.execute(stmt).scalar_one() or 0)


def list_recent(session: Session, *, limit: int = 50) -> list[PointsEntry]:
    """Return the newest points entries, newest first."""
    stmt = select(PointsEntry).order_by(PointsEntry.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars())


def count_since(session: Session, ts: datetime) -> int:
    """Count points entries created after `ts`."""
    stmt = select(func.count(PointsEntry.id)).where(PointsEntry.created_at > ts)
    return int(session.execute(stmt).scalar_one() or 0)


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
    period_key: str | None = None,
    task_id: uuid.UUID | None = None,
) -> PointsEntry:
    entry = PointsEntry(
        source=source,
        factor=factor,
        quantity=quantity,
        amount=_whole(amount),
        section=section,
        action_name=action_name,
        period_key=period_key,
        task_id=task_id,
    )
    session.add(entry)
    session.commit()
    return entry


def has_penalty_entry(
    session: Session,
    *,
    task_id: uuid.UUID,
    action_name: str,
    period_key: str,
) -> bool:
    stmt = (
        select(func.count())
        .select_from(PointsEntry)
        .where(
            PointsEntry.source == "penalty",
            PointsEntry.task_id == task_id,
            PointsEntry.action_name == action_name,
            PointsEntry.period_key == period_key,
        )
    )
    return int(session.execute(stmt).scalar_one() or 0) > 0


def add_penalty_entry_once(
    session: Session,
    *,
    task_id: uuid.UUID,
    action_name: str,
    period_key: str,
    amount: float,
    section: str = "overdue",
) -> PointsEntry | None:
    if has_penalty_entry(
        session,
        task_id=task_id,
        action_name=action_name,
        period_key=period_key,
    ):
        return None
    try:
        return add_entry(
            session,
            source="penalty",
            section=section,
            action_name=action_name,
            period_key=period_key,
            task_id=task_id,
            factor=float(amount),
            quantity=1.0,
            amount=float(amount),
        )
    except IntegrityError:
        session.rollback()
        return None
