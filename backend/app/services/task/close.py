"""Close a task — user marks it done.

Flips the latest anchor raw_input to status='closed' and drops the
mirrored calendar event.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.events import publish_input, publish_points, publish_task, publish_task_removed
from app.services.calendar import delete_task_event
from app.services.notify import clear_task_notification
from app.services.points import award_for_task

log = logging.getLogger(__name__)


async def close_task(session: Session, task_id: uuid.UUID) -> None:
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    # Award completion points before any status flip / orphan delete, so we
    # still have the task's estimation in hand. Idempotent per task and
    # best-effort — never let points bookkeeping block closing a task.
    try:
        award_for_task(session, task)
        publish_points(session)
    except Exception:  # noqa: BLE001
        log.exception("points award failed · task=%s", task_id)
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is not None:
        latest.status = "closed"
        latest.processed_at = datetime.now(timezone.utc)
        session.commit()
        publish_task(session, task_id)
        publish_input(session, latest.id)
    else:
        # Orphan task (no non-duplicate raw_input — e.g. anchor promoted
        # away via no_change override). With nothing to flip, drop the
        # row outright so it stops surfacing as "open" by default.
        session.delete(task)
        session.commit()
        publish_task_removed(task_id)
    await delete_task_event(session, task)
    await clear_task_notification(task_id)
