import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.storage import tasks as tasks_store

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[TaskRead]:
    rows = tasks_store.list_(session, status=status_filter, limit=limit)
    return [TaskRead.model_validate(r) for r in rows]


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    return TaskRead.model_validate(row)


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.create(session, payload)
    session.commit()
    return TaskRead.model_validate(row)


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, session: Session = Depends(get_session)
) -> TaskRead:
    row = tasks_store.update(session, task_id, **payload.model_dump(exclude_unset=True))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    session.commit()
    return TaskRead.model_validate(row)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    row = tasks_store.update(session, task_id, status="dismissed")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    session.commit()
