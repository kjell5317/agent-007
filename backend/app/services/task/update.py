"""Save task edits before updating external calendar state."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.clients import tasks as tasks_store
from app.db.models.task import Task
from app.events import publish_task
from app.services.calendar import update_task_event
from app.services.plan import update_task_to_calendar
from app.services.task.split import reflow_children

log = logging.getLogger(__name__)
PLAN_TRIGGER_FIELDS = frozenset({"estimation", "due_date", "location"})


def affected_tasks(session: Session, row: Task, changed: set[str]) -> list[tuple[uuid.UUID, set[str]]]:
    if row.is_container and "due_date" in changed:
        return [(child.id, {"due_date"}) for child in tasks_store.children(session, row.id)]
    if row.parent_task_id and "estimation" in changed:
        return [
            (child.id, {"due_date", "estimation"} if child.id == row.id else {"due_date"})
            for child in tasks_store.children(session, row.parent_task_id)
        ]
    return [] if row.is_container else [(row.id, changed)]


async def sync_external(session: Session, updates: list[tuple[uuid.UUID, set[str]]]) -> None:
    for task_id, changed in updates:
        row = tasks_store.get(session, task_id)
        if row is None or row.is_container:
            continue
        try:
            if changed & PLAN_TRIGGER_FIELDS:
                await update_task_to_calendar(session, row, changed_fields=changed)
            else:
                await update_task_event(session, row, changed_fields=changed)
        except Exception as exc:  # noqa: BLE001 — the database edit remains saved
            log.exception("calendar sync failed · task=%s", task_id)
            from app.services.notify import notify_error
            await notify_error("Task calendar update failed", exc, context=row.title)


async def update_task(
    session: Session,
    task_id: uuid.UUID,
    fields: dict[str, Any],
    *,
    cascade_deadline: bool = True,
    sync_calendar: bool = True,
    auto_split: bool = True,
) -> Task:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise LookupError("Task not found")
    if "due_date" in fields and fields["due_date"] is None:
        raise ValueError("Due date is required")
    if row.parent_task_id and cascade_deadline and "due_date" in fields:
        raise ValueError("Subtask due dates are set by the parent timeline")
    if row.is_container and "estimation" in fields:
        raise ValueError("Parent duration is calculated from its subtasks")
    if row.parent_task_id and "estimation" in fields and not (fields["estimation"] or 0) > 0:
        raise ValueError("A subtask needs a positive duration")
    row = tasks_store.update(session, task_id, **fields)
    changed = set(fields)
    if cascade_deadline and row.is_container and "due_date" in changed:
        reflow_children(session, row)
    elif row.parent_task_id and "estimation" in changed:
        parent = tasks_store.get(session, row.parent_task_id)
        if parent is not None:
            reflow_children(session, parent)
    else:
        session.commit()
    if auto_split and changed & {"title", "estimation"} and not row.parent_task_id and not row.is_container:
        from app.services.task.split import maybe_split_task
        if await maybe_split_task(session, row):
            publish_task(session, task_id)
            return row
    updates = affected_tasks(session, row, changed)
    publish_task(session, task_id)
    if sync_calendar:
        await sync_external(session, updates)
    return row
