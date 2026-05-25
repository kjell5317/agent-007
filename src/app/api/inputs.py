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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.agent.runner import extract_task_fields
from app.db import get_session
from app.schemas.raw_input import RawInputRead
from app.schemas.task import TaskCreate, TaskPromote, TaskRead
from app.services.google_calendar import add_task_to_calendar
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
    payload: TaskPromote | None = None,
    session: Session = Depends(get_session),
) -> TaskRead:
    """Promote a raw_input the agent skipped into an actual task.

    Body is fully optional. Whichever of `title`, `estimation`, `due_date` the
    caller doesn't supply, the agent fills in by reading the raw_input. Any
    user-provided fields override the agent's guesses.
    """
    raw = raw_inputs.get(session, raw_input_id)
    if raw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Raw input not found")

    user_fields = (
        {k: v for k, v in payload.model_dump().items() if v is not None}
        if payload is not None
        else {}
    )

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
    raw.processed_at = raw.processed_at or datetime.now(timezone.utc)
    trace = dict(raw.agent_trace or {})
    trace["manual_override"] = {
        "outcome": "task_created",
        "task_id": str(task.id),
        "agent_extracted": sorted(agent_fields.keys()) if agent_fields else [],
        "user_provided": sorted(user_fields.keys()),
    }
    raw.agent_trace = trace
    session.commit()
    await add_task_to_calendar(session, task)

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

