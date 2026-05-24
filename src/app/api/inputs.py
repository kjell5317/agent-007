"""Raw-input endpoints.

  * GET  /inputs                     — list raw inputs (filter by status/source)
  * GET  /inputs/{id}                — fetch one
  * POST /inputs/{id}/open_task      — manually promote a raw_input to a task
  * POST /inputs/{source}/webhook    — webhook dispatch (TODO)

Direct creation of raw_inputs goes through the ingestion sources (Gmail/Slack
poll). Manual task entry lives at `POST /tasks`.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas.raw_input import RawInputRead
from app.schemas.task import TaskCreate, TaskRead
from app.storage import raw_inputs, tasks as tasks_store

router = APIRouter(prefix="/inputs", tags=["inputs"])


@router.get("", response_model=list[RawInputRead])
async def list_inputs(
    status_filter: str | None = Query(None, alias="status"),
    source: str | None = Query(None),
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[RawInputRead]:
    rows = raw_inputs.list_(session, status=status_filter, source=source, limit=limit)
    return [RawInputRead.model_validate(r) for r in rows]


@router.get("/{raw_input_id}", response_model=RawInputRead)
async def get_input(
    raw_input_id: uuid.UUID, session: Session = Depends(get_session)
) -> RawInputRead:
    row = raw_inputs.get(session, raw_input_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Raw input not found")
    return RawInputRead.model_validate(row)


@router.post(
    "/{raw_input_id}/open_task",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
async def open_task_from_input(
    raw_input_id: uuid.UUID,
    payload: TaskCreate,
    session: Session = Depends(get_session),
) -> TaskRead:
    """User promotes a raw_input the agent skipped into an actual task."""
    raw = raw_inputs.get(session, raw_input_id)
    if raw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Raw input not found")

    task = tasks_store.create(session, payload)
    raw.status = "open"
    raw.task_id = task.id
    raw.processed_at = raw.processed_at or datetime.now(timezone.utc)
    trace = dict(raw.agent_trace or {})
    trace["manual_override"] = {"outcome": "task_created", "task_id": str(task.id)}
    raw.agent_trace = trace
    session.commit()
    return TaskRead.model_validate(
        {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "link": task.link,
            "due_date": task.due_date,
            "estimation": task.estimation,
            "location": task.location,
            "status": "open",
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
    )


@router.post("/{source}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def source_webhook(
    source: str, request: Request, session: Session = Depends(get_session)
) -> dict:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"webhook for {source!r} not implemented")
