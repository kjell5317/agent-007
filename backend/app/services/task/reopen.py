"""Reopen a previously closed / dismissed / duplicate task."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models.raw_input import RawInput
from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.events import publish_input, publish_task
from app.services.plan import schedule_task


async def enqueue_reopen_task(session: Session, task_id: uuid.UUID) -> RawInput:
    """Create a fresh follow-up input asking the agent to reopen a task.

    The follow-up raw_input must stay separate from the task's lifecycle anchor:
    `run_thread_followup` finalizes its input as `duplicate`, and reusing the
    anchor would erase the status row that currently marks the task closed.
    """
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")

    raw = RawInput(
        source="manual",
        content=(
            "Re-open this task and choose an appropriate new future due date "
            "based on the current task details."
        ),
        source_metadata={
            "manual": True,
            "action": "reopen_task",
            "task_id": str(task.id),
        },
        status="processing",
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)

    publish_input(session, raw.id)
    await _enqueue_followup(raw.id, task.id)
    return raw


async def _enqueue_followup(raw_input_id: uuid.UUID, task_id: uuid.UUID) -> None:
    from app.services.task.queue import enqueue

    await enqueue(raw_input_id, {}, followup_task_id=task_id)


async def reopen_task(session: Session, task_id: uuid.UUID) -> None:
    """Flip the latest anchor raw_input to open and re-create its calendar event.

    This is intentionally direct: agent dispatch calls it after the model chooses
    `update_task(status="open", due_date=...)`.
    """
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is not None:
        latest.status = "open"
        latest.processed_at = datetime.now(timezone.utc)
        session.commit()
        publish_input(session, latest.id)
    # If there's no anchor raw_input (orphan task), the task already
    # surfaces as "open" by default in tasks.list_, so no flip is
    # needed — fall through to re-mirror it on the calendar.
    await schedule_task(session, task)
    publish_task(session, task_id)
