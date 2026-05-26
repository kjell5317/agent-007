"""Raw-input endpoints.

  * GET  /inputs               — list raw inputs (filter by status/source)
  * GET  /inputs/{id}          — fetch one
  * GET  /inputs/unread_count  — inbox unread badge
  * POST /inputs/mark_seen     — reset the unread watermark

Direct creation of raw_inputs goes through the ingestion sources (Gmail/Slack
poll). Manual task entry lives at `POST /tasks`. Promoting an existing
raw_input to a task lives at `POST /tasks/open/{raw_input_id}`.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import state
from app.db import get_session
from app.db.clients import raw_inputs
from app.db.schemas.raw_input import RawInputRead

router = APIRouter(prefix="/inputs", tags=["inputs"])


class UnreadCount(BaseModel):
    count: int
    last_seen_at: datetime


@router.get("", response_model=list[RawInputRead])
async def list_inputs(
    status_filter: str | None = Query(None, alias="status"),
    source: str | None = Query(None),
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[RawInputRead]:
    rows = raw_inputs.list_(session, status=status_filter, source=source, limit=limit)
    return [RawInputRead.from_row(r) for r in rows]


# Static paths must precede the dynamic /{raw_input_id} GET below — FastAPI
# matches in registration order.
@router.get("/unread_count", response_model=UnreadCount)
async def get_unread_count(session: Session = Depends(get_session)) -> UnreadCount:
    return UnreadCount(
        count=raw_inputs.count_since(session, state.last_seen_input_at),
        last_seen_at=state.last_seen_input_at,
    )


@router.post("/mark_seen", response_model=UnreadCount)
async def mark_inputs_seen(session: Session = Depends(get_session)) -> UnreadCount:
    state.last_seen_input_at = datetime.now(timezone.utc)
    return UnreadCount(
        count=raw_inputs.count_since(session, state.last_seen_input_at),
        last_seen_at=state.last_seen_input_at,
    )


@router.get("/{raw_input_id}", response_model=RawInputRead)
async def get_input(
    raw_input_id: uuid.UUID, session: Session = Depends(get_session)
) -> RawInputRead:
    row = raw_inputs.get(session, raw_input_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Raw input not found")
    return RawInputRead.from_row(row)
