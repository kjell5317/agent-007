import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.models.raw_input import RawInput
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.storage import raw_inputs, tasks as tasks_store

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


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(payload: TaskCreate, session: Session = Depends(get_session)) -> TaskRead:
    """Manual task create. Writes a synthetic raw_input(source='manual', status='open')
    so the task's lifecycle is anchored just like agent-created ones."""
    task = tasks_store.create(session, payload)
    synthetic = RawInput(
        source="manual",
        content=payload.description or payload.title,
        source_metadata={"manual": True},
        status="open",
        task_id=task.id,
        processed_at=datetime.now(timezone.utc),
    )
    session.add(synthetic)
    session.commit()
    return _to_read(task, "open")


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, session: Session = Depends(get_session)
) -> TaskRead:
    fields = payload.model_dump(exclude_unset=True)
    row = tasks_store.update(session, task_id, **fields)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    session.commit()
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    return _to_read(row, status_)


@router.post("/{task_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """User marks the task done — insert a synthetic raw_input with status='closed'."""
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    session.add(
        RawInput(
            source="manual",
            content="closed by user",
            source_metadata={"manual": True, "action": "close"},
            status="closed",
            task_id=task_id,
            processed_at=datetime.now(timezone.utc),
            agent_trace={"outcome": "closed", "manual": True},
        )
    )
    session.commit()


@router.post("/{task_id}/not_task", status_code=status.HTTP_204_NO_CONTENT)
async def mark_not_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """User retracts the task — insert a synthetic raw_input with status='not_task'."""
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    session.add(
        RawInput(
            source="manual",
            content="marked as not_task by user",
            source_metadata={"manual": True, "action": "not_task"},
            status="not_task",
            task_id=task_id,
            processed_at=datetime.now(timezone.utc),
            agent_trace={"outcome": "not_task", "manual": True},
        )
    )
    session.commit()
