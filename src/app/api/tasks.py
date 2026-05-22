import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[TaskRead]:
    # TODO: query Task table with optional status filter, order by created_at desc
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "list_tasks not implemented")


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    # TODO: fetch by id, 404 on miss
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "get_task not implemented")


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, session: Session = Depends(get_session)) -> TaskRead:
    # TODO: create Task directly (manual path — bypasses the agent)
    # TODO: compute and store embedding so manual tasks also participate in dedup
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "create_task not implemented")


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, session: Session = Depends(get_session)
) -> TaskRead:
    # TODO: partial update; re-embed if title/description changed
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "update_task not implemented")


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    # TODO: soft-delete (status="dismissed") rather than hard delete; preserves feedback signal
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "delete_task not implemented")
