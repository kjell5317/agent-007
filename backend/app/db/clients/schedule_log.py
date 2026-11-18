"""The schedule log — one row per scheduling-relevant event, the training set
for the slot-preference model and the duration correction."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.scheduling import ScheduleLog

def add(
    session: Session,
    *,
    task_id: uuid.UUID,
    kind: str,
    actor: str,
    slot_start: datetime | None = None,
    slot_end: datetime | None = None,
    prev_start: datetime | None = None,
    prev_end: datetime | None = None,
    solver_run_id: uuid.UUID | None = None,
    features: dict | None = None,
) -> ScheduleLog:
    row = ScheduleLog(
        task_id=task_id,
        kind=kind,
        actor=actor,
        slot_start=slot_start,
        slot_end=slot_end,
        prev_start=prev_start,
        prev_end=prev_end,
        solver_run_id=solver_run_id,
        features=features,
    )
    session.add(row)
    session.flush()
    return row


def for_slot(session: Session, task_id: uuid.UUID, slot_start: datetime) -> list[ScheduleLog]:
    return list(session.scalars(
        select(ScheduleLog)
        .where(ScheduleLog.task_id == task_id, ScheduleLog.slot_start == slot_start)
        .order_by(ScheduleLog.created_at)
    ))


def since(
    session: Session, start: datetime, kinds: Iterable[str] | None = None
) -> list[ScheduleLog]:
    stmt = select(ScheduleLog).where(ScheduleLog.created_at >= start)
    if kinds is not None:
        stmt = stmt.where(ScheduleLog.kind.in_(list(kinds)))
    return list(session.scalars(stmt.order_by(ScheduleLog.created_at)))
