"""Plan-service wrapper for commute planning."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


async def plan_commutes(
    session: Session,
    *,
    week_start: datetime | None = None,
    account_key: str | None = None,
) -> dict:
    from app.services.commute.planner import plan_week_commutes

    return await plan_week_commutes(
        session,
        week_start=week_start,
        account_key=account_key,
    )


async def plan_commutes_window(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    target_event_ids: set[str] | None = None,
    stale_event_ids: set[str] | None = None,
    account_key: str | None = None,
) -> dict:
    from app.services.commute.planner import plan_window_commutes

    return await plan_window_commutes(
        session,
        window_start=window_start,
        window_end=window_end,
        target_event_ids=target_event_ids,
        stale_event_ids=stale_event_ids,
        account_key=account_key,
    )


async def plan_commutes_best_effort(
    session: Session,
    *,
    week_start: datetime | None = None,
    account_key: str | None = None,
) -> None:
    try:
        await plan_commutes(session, week_start=week_start, account_key=account_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.commutes · planning failed err=%s", exc)


async def plan_commutes_window_best_effort(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    target_event_ids: set[str] | None = None,
    stale_event_ids: set[str] | None = None,
    account_key: str | None = None,
) -> None:
    try:
        await plan_commutes_window(
            session,
            window_start=window_start,
            window_end=window_end,
            target_event_ids=target_event_ids,
            stale_event_ids=stale_event_ids,
            account_key=account_key,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.commutes.window · planning failed err=%s", exc)
