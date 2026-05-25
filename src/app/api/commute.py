"""Trigger the commute planner.

  * POST /commute/plan — plan commutes for the next week and write them to
    the user's calendar. Idempotent: re-running replaces previously-planned
    commute events.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_session
from app.services.commute import plan_week_commutes

router = APIRouter(prefix="/commute", tags=["commute"])


@router.post("/plan")
async def plan_commutes(
    account_key: str | None = Query(
        None, description="Pick a non-default Google account to plan against."
    ),
    session: Session = Depends(get_session),
) -> dict:
    return await plan_week_commutes(session, account_key=account_key)
