"""Plan-service wrapper for commute planning.

Every commute entry point runs under the same process-wide scheduling lock
as `schedule_task`: leg writes and the task reschedules they trigger read a
live calendar snapshot, so they must be atomic with any concurrent task
placement. Callers already inside the lock (the scheduler itself) pass
`_depth > 0` to skip re-acquisition — the lock isn't reentrant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings

log = logging.getLogger(__name__)


def commute_window_margin() -> timedelta:
    """How far around a task slot commute work can reach — sized so the
    widest leg + home-layover round trip still falls inside the window."""
    settings = get_settings()
    return timedelta(
        minutes=max(
            120,
            2 * settings.commute_bike_max_minutes
            + settings.commute_home_layover_minutes
            + 2 * settings.commute_event_buffer_minutes,
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
    account_key: str | None = None,
    _depth: int = 0,
) -> dict:
    if not get_settings().commute_enabled:
        return _disabled_summary()
    from app.services.commute.planner import plan_window_commutes
    from app.services.plan.schedule import _schedule_lock

    if _depth > 0:
        return await plan_window_commutes(
            session,
            window_start=window_start,
            window_end=window_end,
            account_key=account_key,
            _depth=_depth,
        )
    async with _schedule_lock:
        return await plan_window_commutes(
            session,
            window_start=window_start,
            window_end=window_end,
            account_key=account_key,
            _depth=1,
        )


async def refresh_commutes_for_weather(
    session: Session,
    *,
    account_key: str | None = None,
) -> dict:
    if not get_settings().commute_enabled:
        return _disabled_summary()
    from app.services.commute.planner import refresh_weather_sensitive_commutes
    from app.services.plan.schedule import _schedule_lock

    async with _schedule_lock:
        return await refresh_weather_sensitive_commutes(session, account_key=account_key, _depth=1)


async def cleanup_past_commute_legs(
    session: Session,
    *,
    account_key: str | None = None,
) -> int:
    if not get_settings().commute_enabled:
        return 0
    from app.services.commute.planner import delete_past_commute_legs
    from app.services.plan.schedule import _schedule_lock

    async with _schedule_lock:
        return await delete_past_commute_legs(session, account_key=account_key)


async def plan_commutes_window_best_effort(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    account_key: str | None = None,
    _depth: int = 0,
) -> None:
    if not get_settings().commute_enabled:
        return
    try:
        await plan_commutes_window(
            session,
            window_start=window_start,
            window_end=window_end,
            account_key=account_key,
            _depth=_depth,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.commute · window planning failed err=%s", exc)
