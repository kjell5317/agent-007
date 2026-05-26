"""Reopen a previously closed / dismissed / duplicate task.

Flips the latest anchor raw_input back to status='open' and re-creates
its calendar event.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.services.plan import schedule


async def reopen_task(session: Session, task_id: uuid.UUID) -> None:
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise LookupError("Task has no raw_input to update")
    latest.status = "open"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()
    await schedule(session, task)
