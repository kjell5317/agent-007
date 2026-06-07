"""Points awarding — action submissions and task-completion bonuses.

Config (sections + `task_done_factor`) lives in `app.points`; the running
total and per-event ledger live in the `points_entries` table. The factor is
always read from the server-side config — callers only name an action and a
quantity, so the client can't inflate its own score.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db.clients import points as points_store
from app.points import find_action, load_points_config

log = logging.getLogger(__name__)


def submit_action(session: Session, *, section: str, name: str, quantity: float | None) -> float:
    """Record an action submission and return the new total.

    Raises `LookupError` when the action isn't configured, and `ValueError`
    when a unit-bearing action is submitted without a positive quantity.
    """
    action = find_action(section, name)
    if action is None:
        raise LookupError(f"No configured action {name!r} in section {section!r}")

    if action.unit is None:
        qty = 1.0
    elif quantity is None or quantity <= 0:
        raise ValueError("This action requires a positive quantity.")
    else:
        qty = float(quantity)

    points_store.add_entry(
        session,
        source="action",
        section=section,
        action_name=name,
        factor=action.factor,
        quantity=qty,
        amount=action.factor * qty,
    )
    return points_store.total(session)


def award_for_task(session: Session, task) -> None:
    """Award `task_done_factor × estimated minutes` for a completed task.

    No-op when the factor is 0 (disabled), the task has no estimation, or the
    task was already awarded (so a reopen→close cycle doesn't double-count). A
    negative factor is allowed and subtracts points on completion.
    """
    factor = load_points_config().task_done_factor
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
