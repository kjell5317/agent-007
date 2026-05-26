"""Task endpoints.

  * GET    /tasks                       — list tasks
  * GET    /tasks/{task_id}             — fetch one
  * POST   /tasks                       — manual create (async via queue)
  * POST   /tasks/open/{raw_input_id}   — promote a raw_input to a task
  * PATCH  /tasks/{task_id}             — edit fields
  * POST   /tasks/{task_id}/close       — mark done
  * POST   /tasks/{task_id}/not_task    — dismiss
  * POST   /tasks/{task_id}/reopen      — revive a closed/dismissed task

All business logic lives in `app.services.task.*` — this file is just
the HTTP surface.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.clients import tasks as tasks_store
from app.db.schemas.task import TaskCreationAccepted, TaskPromote, TaskRead, TaskUpdate
from app.services.task.close import close_task as close_task_svc
from app.services.task.create import create_manual_task
from app.services.task.dismiss import dismiss_task
from app.services.task.open import open_task_from_input
from app.services.task.reopen import reopen_task as reopen_task_svc
from app.services.task.update import update_task as update_task_svc

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _to_read(task, status_: str) -> TaskRead:
    return TaskRead.model_validate(
        {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "link": task.link,
            "due_date": task.due_date,
            "estimation": task.estimation,
            "location": task.location,
            "label": task.label,
            "ai_doable": task.ai_doable,
            "status": status_,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
    )


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
    user_fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        raw = await create_manual_task(session, user_fields)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return TaskCreationAccepted(raw_input_id=raw.id, status="processing")


@router.post(
    "/open/{raw_input_id}",
    response_model=TaskCreationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_task_from_input(
    raw_input_id: uuid.UUID,
    payload: TaskPromote | None = None,
    session: Session = Depends(get_session),
) -> TaskCreationAccepted:
    """Enqueue a manual override that turns an already-processed raw_input
    into a task. Returns immediately; the client polls
    `GET /inputs/{raw_input_id}` until the row gains a `task_id`."""
    user_fields = (
        {k: v for k, v in payload.model_dump().items() if v is not None}
        if payload is not None
        else {}
    )
    try:
        await open_task_from_input(session, raw_input_id, user_fields)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return TaskCreationAccepted(raw_input_id=raw_input_id, status="processing")


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, session: Session = Depends(get_session)
) -> TaskRead:
    fields = payload.model_dump(exclude_unset=True)
    try:
        row = await update_task_svc(session, task_id, fields)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    return _to_read(row, status_)


@router.post("/{task_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    try:
        await close_task_svc(session, task_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{task_id}/not_task", status_code=status.HTTP_204_NO_CONTENT)
async def mark_not_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    try:
        await dismiss_task(session, task_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{task_id}/reopen", status_code=status.HTTP_204_NO_CONTENT)
async def reopen_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    try:
        await reopen_task_svc(session, task_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
