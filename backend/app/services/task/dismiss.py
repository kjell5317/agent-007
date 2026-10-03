"""Dismiss a task while retaining its linked input history for reopening."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.db.models.raw_input import RawInput
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
        # processed_at belongs to ingestion, not to this manual state change.
    else:
        session.delete(task)
    session.commit()
    if latest_id is not None:
        publish_task(session, task_id)
        publish_input(session, latest_id)
    else:
        publish_task_removed(task_id)


def dismiss_container(session: Session, task_id: uuid.UUID) -> list[uuid.UUID]:
    """Persist the whole group before returning to the client."""
    parent = tasks_store.get(session, task_id)
    if parent is None:
        raise LookupError("Task not found")
    if not parent.is_container:
        raise ValueError("Task is not a parent")
    group = [parent, *tasks_store.children(session, task_id)]
    changed: list[tuple[uuid.UUID, uuid.UUID]] = []
    for task in group:
        latest = raw_inputs_store.latest_for_task(session, task.id)
        if latest is None:
            # An inputless task has no lifecycle state. Create an anchor so it
            # remains dismissed and its calendar mirror can still be removed.
            latest = RawInput(
                source="manual", content=task.title, source_metadata={},
                task_id=task.id, status="not_task",
            )
            session.add(latest)
            session.flush()
            changed.append((task.id, latest.id))
        else:
            latest.status = "not_task"
            changed.append((task.id, latest.id))
    session.commit()
    for changed_id, input_id in changed:
        publish_task(session, changed_id)
        publish_input(session, input_id)
    return [task_id for task_id, _ in changed]


async def cleanup_dismissed_container(task_ids: list[uuid.UUID]) -> None:
    """Remove calendar and notification side effects after the HTTP response."""
    from app.services.notify import notify_error

    with SessionLocal() as session:
        for task_id in task_ids:
            task = tasks_store.get(session, task_id)
            vacated = vacated_commute_window(task) if task else None
            try:
                if task:
                    await delete_task_event(session, task)
                    if task.calendar_event_id and get_settings().google_calendar_id:
                        raise RuntimeError(f"Could not remove calendar event for {task.title}")
            except Exception as exc:  # noqa: BLE001
                await notify_error("Task deletion cleanup failed", exc, context=str(task_id))
            try:
                await clear_task_notification(task_id)
            except Exception as exc:  # noqa: BLE001
                await notify_error("Task deletion cleanup failed", exc, context=str(task_id))
            try:
                await replan_vacated_window(session, vacated)
            except Exception as exc:  # noqa: BLE001
                await notify_error("Task deletion cleanup failed", exc, context=str(task_id))
