"""Task update flow.

Apply the patch to the row, then push the change to the calendar mirror
and re-plan commutes when needed.

Calendar wiring follows the house rule "only the plan service touches
calendar" — except for the explicit fast path here: when none of
`PLAN_TRIGGER_FIELDS` changed (the user just renamed the task, fixed a
typo, etc.), we call `calendar.update_task_event` directly without
re-running the planner. Anything that could shift the slot
(estimation / due_date / location) goes through
`plan.update_task_to_calendar`.

Calendar + commute side effects are best-effort — they log and swallow
rather than rolling back the DB update.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.clients import tasks as tasks_store
from app.db.models.task import Task
from app.services.calendar import update_task_event
from app.services.plan import update_task_to_calendar

log = logging.getLogger(__name__)

# Fields that, when changed, need the planner to re-run (slot + routing affected).
PLAN_TRIGGER_FIELDS: frozenset[str] = frozenset({"estimation", "due_date", "location"})


async def update_task(
    session: Session,
    task_id: uuid.UUID,
    fields: dict[str, Any],
) -> Task:
    """Patch `task_id` with `fields`, then sync calendar + (maybe) commutes.

    Raises `LookupError` when the task doesn't exist.
    """
    row = tasks_store.update(session, task_id, **fields)
    if row is None:
        raise LookupError("Task not found")
    session.commit()

    changed = set(fields.keys())
    if changed & PLAN_TRIGGER_FIELDS:
        await update_task_to_calendar(session, row, changed_fields=changed)
    else:
        await update_task_event(session, row, changed_fields=changed)

    return row
