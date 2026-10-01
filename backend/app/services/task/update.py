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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.clients import tasks as tasks_store
from app.db.models.task import Task
from app.events import publish_task
from app.services.calendar import update_task_event
from app.services.plan import update_task_to_calendar
from app.services.task.split import maybe_split_task, refresh_child_deadlines, shift_group_deadlines

log = logging.getLogger(__name__)

# Fields that, when changed, need the planner to re-run (slot + routing affected).
PLAN_TRIGGER_FIELDS: frozenset[str] = frozenset({"estimation", "due_date", "location"})


async def update_task(
    session: Session,
    task_id: uuid.UUID,
    fields: dict[str, Any],
    *,
    cascade_deadline: bool = True,
) -> Task:
    """Patch `task_id` with `fields`, then sync calendar + (maybe) commutes.

    Raises `LookupError` when the task doesn't exist.
    """
    original = tasks_store.get(session, task_id) if cascade_deadline and "due_date" in fields else None
    old_due: datetime | None = original.due_date if original is not None else None
    if original is not None and getattr(original, "is_container", False):
        children = tasks_store.children(session, task_id)
        if children:
            old_due = children[-1].due_date
    row = tasks_store.update(session, task_id, **fields)
    if row is None:
        raise LookupError("Task not found")
    session.commit()

    changed = set(fields.keys())
    deadline_delta = (
        row.due_date - old_due
        if cascade_deadline and "due_date" in changed and old_due is not None and row.due_date is not None
        else None
    )
    if getattr(row, "is_container", False):
        if cascade_deadline and "due_date" in changed:
            await shift_group_deadlines(session, row, deadline_delta or timedelta())
        publish_task(session, task_id)
        return row
    if changed & {"title", "estimation"} and not getattr(row, "parent_task_id", None):
        if await maybe_split_task(session, row):
            publish_task(session, task_id)
            return row
    if changed & PLAN_TRIGGER_FIELDS:
        await update_task_to_calendar(session, row, changed_fields=changed)
    else:
        await update_task_event(session, row, changed_fields=changed)

    if getattr(row, "parent_task_id", None) and cascade_deadline and "due_date" in changed:
        parent = tasks_store.get(session, row.parent_task_id)
        if parent is not None:
            await shift_group_deadlines(
                session, parent, deadline_delta or timedelta(), edited_child_id=row.id
            )
    elif getattr(row, "parent_task_id", None) and "estimation" in changed:
        parent = tasks_store.get(session, row.parent_task_id)
        if parent is not None:
            await refresh_child_deadlines(session, parent)

    publish_task(session, task_id)
    return row
