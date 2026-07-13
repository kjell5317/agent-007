"""Points endpoints — a running score plus recent ledger changes.

  * GET  /points          — current total
  * POST /points/adjust    — add a signed amount, returns the new total
  * GET  /points/log      — entries since the points log was last viewed
  * POST /points/log/mark_seen — reset the points-log watermark

`POST /points/adjust` is modeled on the notification-action webhook: it's
exempt from the email-allowlist middleware so Home Assistant can call it with
the shared `HOME_ASSISTANT_ACTION_SECRET` (via `X-Notify-Secret` header or
`?secret=`). A logged-in browser session (the topbar modal) is accepted too,
so the secret never has to live in frontend code.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import state
from app.config import get_settings
from app.db import get_session
from app.db.clients import points as points_store
from app.db.models.points_entry import PointsEntry
from app.services.points import adjust_points

router = APIRouter(prefix="/points", tags=["points"])


class TotalRead(BaseModel):
    total: int


class AdjustPayload(BaseModel):
    amount: float
    caller: str | None = Field(None, max_length=32)
    reason: str | None = Field(None, max_length=128)


class PointsLogEntryRead(BaseModel):
    id: uuid.UUID
    amount: int
    source: str
    reason: str
    caller: str | None
    task_id: uuid.UUID | None
    created_at: datetime


class PointsLogRead(BaseModel):
    entries: list[PointsLogEntryRead]
    count: int
    last_seen_at: datetime
    has_more: bool


class PointsLogSeenRead(BaseModel):
    count: int
    last_seen_at: datetime


def _check_access(request: Request) -> None:
    settings = get_settings()
    email = request.session.get("email") if hasattr(request, "session") else None
    if email and email.lower() in settings.auth_allowed_emails:
        return
    expected = settings.home_assistant_action_secret
    if not expected:
        return
    provided = request.headers.get("x-notify-secret") or request.query_params.get("secret")
    if provided != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid notify secret")


@router.get("", response_model=TotalRead)
def get_points(session: Session = Depends(get_session)) -> TotalRead:
    return TotalRead(total=points_store.total(session))


# The log is a history, not an unread feed: viewing it (mark_seen) must not
# empty it, and a process restart (which resets the in-memory watermark) must
# not hide older entries. The watermark only drives `count` — the unseen
# badge. We fetch one extra row for explicit `has_more` pagination while keeping
# the default response padded to a useful minimum.
MIN_LOG_ENTRIES = 10


@router.get("/log", response_model=PointsLogRead)
def get_points_log(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    session: Session = Depends(get_session),
) -> PointsLogRead:
    _check_access(request)
    unseen = points_store.count_since(session, state.last_seen_points_log_at)
    entries = points_store.list_recent(
        session, limit=min(max(MIN_LOG_ENTRIES, limit) + 1, 201)
    )
    has_more = len(entries) > limit
    return PointsLogRead(
        entries=[_log_entry(e) for e in entries[:limit]],
        count=unseen,
        last_seen_at=state.last_seen_points_log_at,
        has_more=has_more,
    )


@router.post("/log/mark_seen", response_model=PointsLogSeenRead)
def mark_points_log_seen(
    request: Request,
    session: Session = Depends(get_session),
) -> PointsLogSeenRead:
    _check_access(request)
    state.last_seen_points_log_at = datetime.now(timezone.utc)
    return PointsLogSeenRead(
        count=points_store.count_since(session, state.last_seen_points_log_at),
        last_seen_at=state.last_seen_points_log_at,
    )


@router.post("/adjust", response_model=TotalRead)
def adjust(
    payload: AdjustPayload,
    request: Request,
    session: Session = Depends(get_session),
) -> TotalRead:
    _check_access(request)
    if payload.amount == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "amount must be non-zero")
    new_total = adjust_points(
        session,
        payload.amount,
        caller=payload.caller,
        reason=payload.reason,
    )
    return TotalRead(total=new_total)


def _log_entry(entry: PointsEntry) -> PointsLogEntryRead:
    return PointsLogEntryRead(
        id=entry.id,
        amount=entry.amount,
        source=entry.source,
        reason=_reason_for(entry),
        caller=_caller_for(entry),
        task_id=entry.task_id,
        created_at=entry.created_at,
    )


def _reason_for(entry: PointsEntry) -> str:
    action = (entry.action_name or "").strip()
    if entry.source == "manual":
        caller = (entry.section or "").strip()
        return action or caller or "Manual adjustment"
    if entry.source == "task":
        return action or "Completed task"
    if entry.source == "penalty":
        return _penalty_reason(action, entry.period_key)
    return action or entry.source.replace("_", " ").title()


def _caller_for(entry: PointsEntry) -> str | None:
    if entry.source == "manual":
        return (entry.section or "").strip() or None
    if entry.source == "task":
        return "Task completion"
    if entry.source == "penalty":
        return "Penalty"
    return entry.source.replace("_", " ").title()


def _penalty_reason(action: str, period_key: str | None) -> str:
    if action == "scheduled_overdue_reschedule":
        return "Scheduled task overdue"
    if action == "due_overdue_hour":
        label = _due_penalty_label(period_key)
        return f"Due task overdue{label}"
    if action:
        return action.replace("_", " ").capitalize()
    return "Points penalty"


def _due_penalty_label(period_key: str | None) -> str:
    if not period_key or ":h:" not in period_key:
        return ""
    try:
        hour = int(period_key.rsplit(":h:", 1)[1])
    except ValueError:
        return ""
    return f" ({hour + 1}h)" if hour >= 0 else ""
