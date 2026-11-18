from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.work import DayContext, WorkSession


def running(session: Session) -> WorkSession | None:
    stmt = (
        select(WorkSession)
        .where(WorkSession.ended_at.is_(None))
        .order_by(WorkSession.started_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


def open_for_task(session: Session, task_id: uuid.UUID) -> WorkSession | None:
    stmt = select(WorkSession).where(
        WorkSession.task_id == task_id, WorkSession.ended_at.is_(None)
    )
    return session.execute(stmt).scalars().first()


def for_task(session: Session, task_id: uuid.UUID) -> list[WorkSession]:
    stmt = (
        select(WorkSession)
        .where(WorkSession.task_id == task_id)
        .order_by(WorkSession.started_at)
    )
    return list(session.execute(stmt).scalars())


def for_tasks(session: Session, task_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[WorkSession]]:
    out: dict[uuid.UUID, list[WorkSession]] = defaultdict(list)
    if not task_ids:
        return out
    stmt = (
        select(WorkSession)
        .where(WorkSession.task_id.in_(task_ids))
        .order_by(WorkSession.started_at)
    )
    for row in session.execute(stmt).scalars():
        out[row.task_id].append(row)
    return out


def since(session: Session, start: datetime) -> list[WorkSession]:
    stmt = (
        select(WorkSession)
        .where(WorkSession.started_at >= start)
        .order_by(WorkSession.started_at)
    )
    return list(session.execute(stmt).scalars())


def start(session: Session, task_id: uuid.UUID, at: datetime) -> WorkSession:
    row = WorkSession(task_id=task_id, started_at=at, source="live")
    session.add(row)
    session.flush()
    return row


def end(session: Session, row: WorkSession, at: datetime, reason: str) -> None:
    row.ended_at = max(at, row.started_at)
    row.end_reason = reason
    session.flush()


def add_logged(
    session: Session, task_id: uuid.UUID, started_at: datetime, ended_at: datetime, reason: str
) -> WorkSession:
    row = WorkSession(
        task_id=task_id, started_at=started_at, ended_at=ended_at,
        end_reason=reason, source="retro",
    )
    session.add(row)
    session.flush()
    return row


def day_context(session: Session, day: date) -> DayContext | None:
    return session.get(DayContext, day)


def record_sleep(
    session: Session, day: date, *, sleep_start: datetime, wake_at: datetime, sleep_minutes: int
) -> DayContext:
    row = session.get(DayContext, day) or DayContext(day=day)
    row.sleep_start = sleep_start
    row.wake_at = wake_at
    row.sleep_minutes = sleep_minutes
    session.add(row)
    session.flush()
    return row
