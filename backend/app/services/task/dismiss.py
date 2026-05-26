"""Dismiss a task — user retracts it as not actually a task for them.

Flips the latest anchor raw_input to status='not_task' and drops the
mirrored calendar event.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.services.calendar import delete_task_event


async def dismiss_task(session: Session, task_id: uuid.UUID) -> None:
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise LookupError("Task has no raw_input to update")
    latest.status = "not_task"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()
    await delete_task_event(session, task)
