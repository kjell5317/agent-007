"""Dismiss a task — user retracts it as not actually a task for them.

Flips the anchor raw_input to status='not_task', drops the calendar
mirror, and deletes the task row outright. The end shape matches an
agent-auto `mark_not_task` (raw_input status='not_task', task_id NULL),
so the inbox card can offer a clean "Make a task" override without the
old backlink getting in the way.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.services.calendar import delete_task_event
from app.services.notify import clear_task_notification


async def dismiss_task(session: Session, task_id: uuid.UUID) -> None:
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    # Best-effort calendar cleanup first, while task.calendar_event_id is
    # still readable. Errors are swallowed inside delete_task_event.
    await delete_task_event(session, task)
    await clear_task_notification(task_id)
    # If an anchor raw_input still exists, flip it to `not_task` so the
    # inbox card reflects the dismissal. Orphan tasks (anchor promoted
    # away via no_change override, etc.) skip this — the deletion below
    # is enough to make them disappear.
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is not None:
        latest.status = "not_task"
        latest.processed_at = datetime.now(timezone.utc)
    # Drop the task row. Without this, listing defaults a task with no
    # non-duplicate raw_input back to "open" (see tasks.list_), so the
    # dismissed task would silently reappear. The raw_inputs FK is
    # `ON DELETE SET NULL`, so every backlink (anchor + follow-ups)
    # clears in one statement.
    session.delete(task)
    session.commit()
