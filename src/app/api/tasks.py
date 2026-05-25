import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.agent.runner import extract_task_fields
from app.db import get_session
from app.models.raw_input import RawInput
from app.schemas.task import TaskCreate, TaskPromote, TaskRead, TaskUpdate
from app.services.google_calendar import add_task_to_calendar, update_task_in_calendar
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
async def create_task(payload: TaskPromote, session: Session = Depends(get_session)) -> TaskRead:
    """Manual task create. Mirrors POST /inputs/{id}/open_task: a synthetic
    raw_input is the anchor, and the agent fills any of the required fields
    (title/estimation/due_date) the user didn't supply. User-provided fields
    always override the agent's guesses."""
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
    session.flush()

    needs_agent = not all(user_fields.get(k) for k in ("title", "estimation", "due_date"))
    agent_fields: dict = {}
    if needs_agent:
        agent_fields = await extract_task_fields(raw)

    merged = {**agent_fields, **user_fields}
    task = tasks_store.create(
        session,
        TaskCreate(
            title=merged["title"],
            description=merged.get("description"),
            estimation=merged.get("estimation"),
            due_date=merged.get("due_date"),
            location=merged.get("location"),
            link=merged.get("link"),
        ),
    )

    raw.status = "open"
    raw.task_id = task.id
    raw.processed_at = datetime.now(timezone.utc)
    raw.agent_trace = {
        "outcome": "task_created",
        "branch": "manual",
        "task_id": str(task.id),
        "agent_extracted": sorted(agent_fields.keys()) if agent_fields else [],
        "user_provided": sorted(user_fields.keys()),
    }
    session.commit()
    await add_task_to_calendar(session, task)
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
    await update_task_in_calendar(session, row)
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    return _to_read(row, status_)


@router.post("/{task_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """User marks the task done — flip the latest raw_input's status to 'closed'."""
    if tasks_store.get(session, task_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task has no raw_input to update")
    latest.status = "closed"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()


@router.post("/{task_id}/not_task", status_code=status.HTTP_204_NO_CONTENT)
async def mark_not_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """User retracts the task — flip the latest raw_input's status to 'not_task'."""
    if tasks_store.get(session, task_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task has no raw_input to update")
    latest.status = "not_task"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()


@router.post("/{task_id}/reopen", status_code=status.HTTP_204_NO_CONTENT)
async def reopen_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    """Re-open a previously closed / not_task / duplicate task by flipping the
    latest anchor raw_input back to 'open'."""
    if tasks_store.get(session, task_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task has no raw_input to update")
    latest.status = "open"
    latest.processed_at = datetime.now(timezone.utc)
    session.commit()
