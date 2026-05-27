"""Plan-service wrapper for commute planning."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings

log = logging.getLogger(__name__)


def commute_window_margin() -> timedelta:
    """How far on either side of a task slot to sweep for commute work.

    Sized so the widest possible commute leg + home-layover round-trip can
    still fall fully inside the queried window.
    """
    settings = get_settings()
    return timedelta(
        minutes=max(
            settings.commute_bike_max_minutes,
            settings.commute_home_layover_minutes * 2,
            settings.commute_event_buffer_minutes,
        )
    )


def _disabled_summary() -> dict:
    return {
        "planned": 0,
        "skipped_online": 0,
        "skipped_unroutable": 0,
        "rescheduled_tasks": 0,
        "errors": [],
    }


async def plan_commutes_window(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    target_event_ids: set[str] | None = None,
    stale_event_ids: set[str] | None = None,
    account_key: str | None = None,
) -> dict:
    if not get_settings().commute_enabled:
        return _disabled_summary()
    from app.services.commute.planner import plan_window_commutes

    return await plan_window_commutes(
        session,
        window_start=window_start,
        window_end=window_end,
        target_event_ids=target_event_ids,
        stale_event_ids=stale_event_ids,
        account_key=account_key,
    )


async def refresh_commutes_for_weather(
    session: Session,
    *,
    account_key: str | None = None,
) -> dict:
    if not get_settings().commute_enabled:
        return _disabled_summary()
    from app.services.commute.planner import refresh_weather_sensitive_commutes

    return await refresh_weather_sensitive_commutes(session, account_key=account_key)


async def plan_commutes_window_best_effort(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    target_event_ids: set[str] | None = None,
    stale_event_ids: set[str] | None = None,
    account_key: str | None = None,
) -> None:
    if not get_settings().commute_enabled:
        return
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
        log.warning("plan.commute · window planning failed err=%s", exc)
