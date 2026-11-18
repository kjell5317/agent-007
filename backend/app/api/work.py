"""Work sessions on tasks.

  * POST /tasks/{id}/start            — start (pauses another running task)
  * POST /tasks/{id}/pause            — {reason: break | waiting | interrupted}
  * POST /tasks/{id}/resume           — a waiting task goes back to the planner
  * POST /tasks/{id}/complete         — end the running session and close
  * POST /tasks/{id}/sessions         — log a span afterwards, optionally completing
  * GET  /tasks/{id}/sessions         — the task's sessions, oldest first
  * POST /tasks/logged                — spontaneous work: create, log and close a task
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_session
from app.api.tasks import task_or_404
from app.db.clients import labels as labels_store
from app.db.clients import work as work_store
from app.db.schemas.task import Effort, TaskRead, WorkType
from app.events import read_task
from app.services.task import work

router = APIRouter(prefix="/tasks", tags=["work"])


class PauseBody(BaseModel):
    reason: Literal["break", "waiting", "interrupted"]


class LogBody(BaseModel):
    started_at: AwareDatetime
    ended_at: AwareDatetime
    complete: bool = True


class LoggedTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    label: str | None = None
    work_type: WorkType | None = None
    effort: Effort | None = None


class WorkSessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    source: str


@router.post("/logged", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def log_spontaneous(body: LoggedTaskBody, session: Session = Depends(get_session)) -> TaskRead:
    if body.label and not labels_store.exists(session, body.label):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown label")
    try:
        task = await work.log_spontaneous(
            session, title=body.title, started_at=body.started_at, ended_at=body.ended_at,
            label=body.label, work_type=body.work_type, effort=body.effort,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return read_task(session, task)


@router.post("/{task_id}/start", response_model=TaskRead)
async def start(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    task = task_or_404(session, task_id)
    try:
        await work.start_task(session, task)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return read_task(session, task)


@router.post("/{task_id}/pause", response_model=TaskRead)
async def pause(
    task_id: uuid.UUID, body: PauseBody, session: Session = Depends(get_session)
) -> TaskRead:
    task = task_or_404(session, task_id)
    try:
        await work.pause_task(session, task, body.reason)
    except LookupError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return read_task(session, task)


@router.post("/{task_id}/resume", response_model=TaskRead)
async def resume(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    task = task_or_404(session, task_id)
    await work.resume_task(session, task)
    return read_task(session, task)


@router.post("/{task_id}/complete", response_model=TaskRead)
async def complete(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    task = task_or_404(session, task_id)
    try:
        await work.complete_task(session, task)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return read_task(session, task)


@router.post("/{task_id}/sessions", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def log_session(
    task_id: uuid.UUID, body: LogBody, session: Session = Depends(get_session)
) -> TaskRead:
    task = task_or_404(session, task_id)
    try:
        await work.log_work(
            session, task, started_at=body.started_at,
            ended_at=body.ended_at, complete=body.complete,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return read_task(session, task)


@router.get("/{task_id}/sessions", response_model=list[WorkSessionRead])
def list_sessions(task_id: uuid.UUID, session: Session = Depends(get_session)) -> list[WorkSessionRead]:
    task_or_404(session, task_id)
    return [WorkSessionRead.model_validate(row) for row in work_store.for_task(session, task_id)]
