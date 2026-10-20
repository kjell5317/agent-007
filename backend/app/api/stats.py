"""Scheduling metrics.

  * GET /stats/scheduling — per week: share of slots completed in their slot,
    user moves, Not-done and no-response rates; plus the active preference
    model's version and held-out fit, and the deferral capacity share.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_session
from app.services.learn.jobs import scheduling_stats
from app.timezones import user_tz

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/scheduling")
def get_scheduling_stats(
    weeks: int = Query(8, ge=1, le=52), session: Session = Depends(get_session)
) -> dict:
    return scheduling_stats(session, now=datetime.now(user_tz()), weeks=weeks)
