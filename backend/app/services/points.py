"""Points awarding — manual adjustments and task-completion bonuses.

The running total and per-event ledger live in the `points_entries` table.
Task completion awards `points_task_done_factor × estimated minutes` for
normal tasks and `0.1 × estimated minutes` for kotx-linked tasks; manual
adjustments (from the topbar modal or Home Assistant) add a signed amount
directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import points as points_store
from app.events import publish_points

log = logging.getLogger(__name__)

PENALTY_POINTS = 10
KOTX_TASK_DONE_FACTOR = 0.1
SCHEDULED_OVERDUE_ACTION = "scheduled_overdue_reschedule"
DUE_OVERDUE_ACTION = "due_overdue_hour"


def adjust_points(
    session: Session,
    amount: float,
    *,
    caller: str | None = None,
    reason: str | None = None,
) -> float:
    """Add a signed amount to the ledger and return the new total."""
    points_store.add_entry(
        session,
        source="manual",
        section=_clean_manual_field(caller, limit=32),
        action_name=_clean_manual_field(reason, limit=128),
        factor=float(amount),
        quantity=1.0,
        amount=float(amount),
    )
    log.info("points · manual adjust amount=%s caller=%s", amount, caller)
    publish_points(session)
    return points_store.total(session)


def _clean_manual_field(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned[:limit] or None


def award_for_task(session: Session, task) -> bool:
    """Award completion points for a completed task.

    Returns whether a ledger entry was inserted. No-op when the factor is 0
    (disabled), the task has no estimation, or the task was already awarded
    (so a reopen→close cycle doesn't double-count). A negative factor is
    allowed and subtracts points on completion.
    """
    factor = (
        KOTX_TASK_DONE_FACTOR
        if getattr(task, "kotx_task_id", None) is not None
        else get_settings().points_task_done_factor
    )
    minutes = task.estimation or 0
    if factor == 0 or minutes <= 0:
        return False
    if points_store.has_task_entry(session, task.id):
        return False
    points_store.add_entry(
        session,
        source="task",
        action_name=(task.title or "")[:128] or None,
        task_id=task.id,
        factor=factor,
        quantity=float(minutes),
        amount=factor * minutes,
    )
    log.info("points · awarded task=%s minutes=%s factor=%s", task.id, minutes, factor)
    return True


def subtract_scheduled_overdue_penalty(session: Session, task, *, scheduled_date: datetime) -> bool:
    period_key = "scheduled:" + _utc_key(scheduled_date)
    entry = points_store.add_penalty_entry_once(
        session,
        task_id=task.id,
        action_name=SCHEDULED_OVERDUE_ACTION,
        period_key=period_key,
        amount=-float(PENALTY_POINTS),
    )
    if entry is None:
        return False
    log.info("points · overdue scheduled penalty task=%s points=%s", task.id, PENALTY_POINTS)
    return True


def subtract_due_overdue_penalties(
    session: Session,
    task,
    *,
    now: datetime,
) -> int:
    due = _as_utc(task.due_date)
    current = _as_utc(now)
    if current < due:
        return 0

    overdue_hours = int((current - due).total_seconds() // timedelta(hours=1).total_seconds())
    inserted = 0
    for hour_index in range(overdue_hours + 1):
        period_key = f"due:{_utc_key(due)}:h:{hour_index}"
        entry = points_store.add_penalty_entry_once(
            session,
            task_id=task.id,
            action_name=DUE_OVERDUE_ACTION,
            period_key=period_key,
            amount=-float(PENALTY_POINTS),
        )
        if entry is not None:
            inserted += 1
    if inserted:
        log.info(
            "points · overdue due penalties task=%s entries=%s points=%s",
            task.id,
            inserted,
            inserted * PENALTY_POINTS,
        )
    return inserted * PENALTY_POINTS


def _utc_key(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
