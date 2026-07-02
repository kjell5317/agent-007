"""Points awarding — manual adjustments and task-completion bonuses.

The running total and per-event ledger live in the `points_entries` table.
Task completion awards `points_task_done_factor × estimated minutes`; manual
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

PENALTY_POINTS = 5
SCHEDULED_OVERDUE_ACTION = "scheduled_overdue_reschedule"
DUE_OVERDUE_ACTION = "due_overdue_hour"


def adjust_points(session: Session, amount: float) -> float:
    """Add a signed amount to the ledger and return the new total."""
    points_store.add_entry(
        session,
        source="manual",
        factor=float(amount),
        quantity=1.0,
        amount=float(amount),
    )
    log.info("points · manual adjust amount=%s", amount)
    publish_points(session)
    return points_store.total(session)


def award_for_task(session: Session, task) -> None:
    """Award `points_task_done_factor × estimated minutes` for a completed task.

    No-op when the factor is 0 (disabled), the task has no estimation, or the
    task was already awarded (so a reopen→close cycle doesn't double-count). A
    negative factor is allowed and subtracts points on completion.
    """
    factor = get_settings().points_task_done_factor
    minutes = task.estimation or 0
    if factor == 0 or minutes <= 0:
        return
    if points_store.has_task_entry(session, task.id):
        return
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
