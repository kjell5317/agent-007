"""The schedule log — one row per scheduling-relevant event, the training set
for the slot-preference model and the duration correction."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.scheduling import ScheduleLog

RESOLUTION_KINDS = frozenset({"still_working", "not_done", "done_confirmed", "no_response"})


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


def by_slot_for_tasks(
    session: Session, task_ids: Iterable[uuid.UUID], kinds: Iterable[str]
) -> dict[tuple[uuid.UUID, datetime], set[str]]:
    """`{(task_id, slot_start): {kinds}}` for the given tasks — one query for
    the check-in sweep and the solver's "awaiting check-in" filter."""
    ids = list(task_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(ScheduleLog.task_id, ScheduleLog.slot_start, ScheduleLog.kind)
        .where(ScheduleLog.task_id.in_(ids), ScheduleLog.kind.in_(list(kinds)))
    ).all()
    out: dict[tuple[uuid.UUID, datetime], set[str]] = {}
    for task_id, slot_start, kind in rows:
        out.setdefault((task_id, slot_start), set()).add(kind)
    return out


def latest_of_kind(
    session: Session, task_id: uuid.UUID, kind: str
) -> ScheduleLog | None:
    return session.scalars(
        select(ScheduleLog)
        .where(ScheduleLog.task_id == task_id, ScheduleLog.kind == kind)
        .order_by(ScheduleLog.created_at.desc())
        .limit(1)
    ).first()


def pending_checkins(session: Session, *, sent_before: datetime) -> list[ScheduleLog]:
    """`checkin_sent` rows older than `sent_before` with no resolution for the
    same slot — the "no answer" sweep."""
    sent = list(session.scalars(
        select(ScheduleLog)
        .where(ScheduleLog.kind == "checkin_sent", ScheduleLog.created_at < sent_before)
    ))
    if not sent:
        return []
    resolved = by_slot_for_tasks(
        session, {row.task_id for row in sent}, RESOLUTION_KINDS | {"closed"}
    )
    return [row for row in sent if not resolved.get((row.task_id, row.slot_start))]


def since(
    session: Session, start: datetime, kinds: Iterable[str] | None = None
) -> list[ScheduleLog]:
    stmt = select(ScheduleLog).where(ScheduleLog.created_at >= start)
    if kinds is not None:
        stmt = stmt.where(ScheduleLog.kind.in_(list(kinds)))
    return list(session.scalars(stmt.order_by(ScheduleLog.created_at)))


def all_for_tasks(session: Session, task_ids: Iterable[uuid.UUID]) -> list[ScheduleLog]:
    ids = list(task_ids)
    if not ids:
        return []
    return list(session.scalars(
        select(ScheduleLog)
        .where(ScheduleLog.task_id.in_(ids))
        .order_by(ScheduleLog.created_at)
    ))
