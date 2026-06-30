"""Points awarding — manual adjustments and task-completion bonuses.

The running total and per-event ledger live in the `points_entries` table.
Task completion awards `points_task_done_factor × estimated minutes`; manual
adjustments (from the topbar modal or Home Assistant) add a signed amount
directly.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import points as points_store
from app.events import publish_points

log = logging.getLogger(__name__)


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
