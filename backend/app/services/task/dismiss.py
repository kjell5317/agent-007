"""Dismiss a task while retaining its linked input history for reopening."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.events import publish_input, publish_task, publish_task_removed
from app.services.calendar import delete_task_event
from app.services.notify import clear_task_notification
from app.services.task.vacate import vacated_commute_window, replan_vacated_window


async def dismiss_task(session: Session, task_id: uuid.UUID) -> None:
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    vacated = vacated_commute_window(task)
    # Best-effort calendar cleanup first, while task.calendar_event_id is
    # still readable. Errors are swallowed inside delete_task_event.
    await delete_task_event(session, task)
    await clear_task_notification(task_id)
    await replan_vacated_window(session, vacated)
    # Keep the task and every input backlink so follow-ups remain grouped and
    # the dismissed task can be reopened. Orphans have no lifecycle anchor and
    # would still appear open by default, so remove only those.
    latest = raw_inputs_store.latest_for_task(session, task_id)
    latest_id = latest.id if latest is not None else None
    if latest is not None:
        latest.status = "not_task"
        latest.processed_at = datetime.now(timezone.utc)
    else:
        session.delete(task)
    session.commit()
    if latest_id is not None:
        publish_task(session, task_id)
        publish_input(session, latest_id)
    else:
        publish_task_removed(task_id)
