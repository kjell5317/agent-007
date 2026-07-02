"""Task endpoints.

  * GET    /tasks                       — list tasks
  * GET    /tasks/{task_id}             — fetch one
  * GET    /tasks/unread_count          — Tasks-tab unread badge
  * POST   /tasks/mark_seen             — reset the unread watermark
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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import state
from app.db import get_session
from app.db.clients import raw_inputs as raw_inputs_store
from app.db.clients import tasks as tasks_store
from app.db.schemas.task import (
    TaskCreationAccepted,
    TaskOpenRequest,
    TaskPromote,
    TaskRawInputRead,
    TaskRead,
    TaskUpdate,
)
from app.events import publish_task
from app.services.kotx import (
    KotxConfigError,
    KotxRunError,
    KotxUnsupportedTaskError,
    create_issue_run,
    has_github_url,
)
from app.services.plan import schedule_task, scheduled_interval_for
from app.services.source_url import source_url_for_raw_input
from app.services.task.close import close_task as close_task_svc
from app.services.task.create import create_manual_task
from app.services.task.dismiss import dismiss_task
from app.services.task.open import open_task_from_input
from app.services.task.reopen import reopen_task as reopen_task_svc
from app.services.task.update import update_task as update_task_svc

router = APIRouter(prefix="/tasks", tags=["tasks"])


class UnreadCount(BaseModel):
    count: int
    last_seen_at: datetime


def _to_read(task, status_: str, is_manual: bool, session: Session) -> TaskRead:
    raw = raw_inputs_store.latest_for_task(session, task.id)
    linked_inputs = raw_inputs_store.list_for_task(session, task.id)
    return TaskRead.build(
        task,
        status_,
        is_manual,
        source_url=source_url_for_raw_input(raw),
        raw_inputs=[
            TaskRawInputRead.build(
                linked,
                source_url=source_url_for_raw_input(linked),
            )
            for linked in linked_inputs
        ],
    )


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[TaskRead]:
    rows = tasks_store.list_(session, status=status_filter, limit=limit)
    manual_map = tasks_store.is_manual_for(session, [t.id for t, _ in rows])
    return [_to_read(t, s, manual_map.get(t.id, False), session) for t, s in rows]


# Static paths must precede the dynamic /{task_id} GET — FastAPI matches in
# registration order, otherwise "unread_count" / "mark_seen" would be parsed
# as a task UUID and 422 out.
@router.get("/unread_count", response_model=UnreadCount)
async def get_unread_count(session: Session = Depends(get_session)) -> UnreadCount:
    return UnreadCount(
        count=tasks_store.count_since(session, state.last_seen_task_at),
        last_seen_at=state.last_seen_task_at,
    )


@router.post("/mark_seen", response_model=UnreadCount)
async def mark_tasks_seen(session: Session = Depends(get_session)) -> UnreadCount:
    state.last_seen_task_at = datetime.now(timezone.utc)
    return UnreadCount(
        count=tasks_store.count_since(session, state.last_seen_task_at),
        last_seen_at=state.last_seen_task_at,
    )


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


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
    payload: TaskOpenRequest | None = None,
    session: Session = Depends(get_session),
) -> TaskCreationAccepted:
    """Enqueue a manual override that turns an already-processed raw_input
    into a task. Returns immediately; the client polls
    `GET /inputs/{raw_input_id}` until the row gains a `task_id`."""
    context_input_ids = payload.context_input_ids if payload is not None else []
    user_fields = (
        {
            k: v
            for k, v in payload.model_dump(exclude={"context_input_ids"}).items()
            if v is not None
        }
        if payload is not None
        else {}
    )
    try:
        await open_task_from_input(
            session, raw_input_id, user_fields, context_input_ids
        )
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
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/reschedule", response_model=TaskRead)
async def reschedule_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")

    result = await schedule_task(session, row, block=scheduled_interval_for(row))
    if result is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Task could not be scheduled")

    publish_task(session, task_id)
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/github_issue", response_model=TaskRead)
async def create_github_issue(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if has_github_url(row.link):
        raise HTTPException(status.HTTP_409_CONFLICT, "Task already has a GitHub URL")

    try:
        run = await create_issue_run(row)
    except KotxUnsupportedTaskError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KotxConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except KotxRunError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    row.link = run.issue_url
    session.commit()
    publish_task(session, task_id)
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


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
