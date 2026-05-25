import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.models.raw_input import RawInput
from app.schemas.task import TaskCreationAccepted, TaskPromote, TaskRead, TaskUpdate
from app.services import task_creation_queue
from app.services.google_calendar import (
    add_task_to_calendar,
    remove_task_from_calendar,
    update_task_in_calendar,
)
from app.storage import raw_inputs as raw_inputs_store, tasks as tasks_store

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _to_read(task, status_: str) -> TaskRead:
    data = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "link": task.link,
        "due_date": task.due_date,
        "estimation": task.estimation,
        "location": task.location,
        "label": task.label,
        "status": status_,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }
    return TaskRead.model_validate(data)


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[TaskRead]:
    rows = tasks_store.list_(session, status=status_filter, limit=limit)
    return [_to_read(t, s) for t, s in rows]


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    return _to_read(row, status_)


@router.post(
    "", response_model=TaskCreationAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def create_task(
    payload: TaskPromote, session: Session = Depends(get_session)
) -> TaskCreationAccepted:
    """Manual task create. Anchors a synthetic raw_input synchronously and
    hands the agent work (extracting title/estimation/due_date, persisting the
    task, mirroring to Calendar) to the in-process queue worker. The client
    polls `GET /inputs/{raw_input_id}` to know when the task is ready, which
    means a second POST can run while a previous one is still processing."""
    user_fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    content = (user_fields.get("description") or user_fields.get("title") or "").strip()
    if not content:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Provide a title or description"
        )

    raw = RawInput(
        source="manual",
        content=content,
        source_metadata={"manual": True},
        status="processing",
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)

    await task_creation_queue.enqueue(raw.id, user_fields)
    return TaskCreationAccepted(raw_input_id=raw.id, status="processing")


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, session: Session = Depends(get_session)
) -> TaskRead:
    fields = payload.model_dump(exclude_unset=True)
    row = tasks_store.update(session, task_id, **fields)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    session.commit()
    await update_task_in_calendar(session, row)
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    return _to_read(row, status_)


@router.post("/{task_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """User marks the task done — flip the latest raw_input's status to 'closed'
    and drop the mirrored calendar event."""
    task = tasks_store.get(session, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task has no raw_input to update")
    latest.status = "closed"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()
    await remove_task_from_calendar(session, task)


@router.post("/{task_id}/not_task", status_code=status.HTTP_204_NO_CONTENT)
async def mark_not_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """User retracts the task — flip the latest raw_input's status to 'not_task'
    and drop the mirrored calendar event."""
    task = tasks_store.get(session, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task has no raw_input to update")
    latest.status = "not_task"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()
    await remove_task_from_calendar(session, task)


@router.post("/{task_id}/reopen", status_code=status.HTTP_204_NO_CONTENT)
async def reopen_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """Re-open a previously closed / not_task / duplicate task by flipping the
    latest anchor raw_input back to 'open' and re-creating its calendar event."""
    task = tasks_store.get(session, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task has no raw_input to update")
    latest.status = "open"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()
    await add_task_to_calendar(session, task)
