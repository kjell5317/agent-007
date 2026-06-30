"""Points endpoints — a single running score.

  * GET  /points          — current total
  * POST /points/adjust    — add a signed amount, returns the new total

`POST /points/adjust` is modeled on the notification-action webhook: it's
exempt from the email-allowlist middleware so Home Assistant can call it with
the shared `HOME_ASSISTANT_ACTION_SECRET` (via `X-Notify-Secret` header or
`?secret=`). A logged-in browser session (the topbar modal) is accepted too,
so the secret never has to live in frontend code.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.db.clients import points as points_store
from app.services.points import adjust_points

router = APIRouter(prefix="/points", tags=["points"])


class TotalRead(BaseModel):
    total: float


class AdjustPayload(BaseModel):
    amount: float


def _check_access(request: Request) -> None:
    settings = get_settings()
    email = request.session.get("email") if hasattr(request, "session") else None
    if email and email.lower() in settings.auth_allowed_emails:
        return
    expected = settings.home_assistant_action_secret
    if not expected:
        return
    provided = (
        request.headers.get("x-notify-secret")
        or request.query_params.get("secret")
    )
    if provided != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid notify secret")


@router.get("", response_model=TotalRead)
def get_points(session: Session = Depends(get_session)) -> TotalRead:
    return TotalRead(total=points_store.total(session))


@router.post("/adjust", response_model=TotalRead)
def adjust(
    payload: AdjustPayload,
    request: Request,
    session: Session = Depends(get_session),
) -> TotalRead:
    _check_access(request)
    if payload.amount == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "amount must be non-zero")
    new_total = adjust_points(session, payload.amount)
    return TotalRead(total=new_total)
