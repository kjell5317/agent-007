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


def solver_routes() -> bool:
    """The solver owns every leg in the horizon: commute inside the model."""
    from app.services.plan.routing import routing_enabled
    from app.services.plan.schedule import scheduler_engine

    return scheduler_engine() == "solver" and routing_enabled()


async def _solve_for_commute(session: Session, reason: str, account_key: str | None) -> dict:
    from app.services.plan.replan import request_replan

    result = await request_replan(session, reason=reason, account_key=account_key)
    summary = _disabled_summary()
    summary["planned"] = len(result.routes or [])
    return summary


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
    if _depth == 0 and solver_routes():
        return await _solve_for_commute(session, "commute_window", account_key)
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
    if solver_routes():
        return await _refresh_solver_weather(session, account_key)
    from app.services.commute.planner import refresh_weather_sensitive_commutes
    from app.services.plan.schedule import _schedule_lock

    async with _schedule_lock:
        return await refresh_weather_sensitive_commutes(session, account_key=account_key, _depth=1)


async def _refresh_solver_weather(session: Session, account_key: str | None) -> dict:
    """Re-solve only when a rain probability for the next 48 h moved by 10
    points or more since the last applied solve."""
    from app.services.commute.weather import blended_rain_pct, geocode, hourly_forecast
    from app.services.plan import replan, routing
    from app.timezones import user_tz

    now = datetime.now(user_tz())
    latlon = await geocode(session, get_settings().home_address)
    if latlon is None:
        return _disabled_summary()
    forecast = await hourly_forecast(
        latlon[0], latlon[1], now, now + timedelta(hours=routing.RAIN_CHANGE_HOURS)
    )
    current = {}
    for hour, row in forecast.items():
        if "rain_pct" not in row:
            continue
        lead = (datetime.fromisoformat(hour).replace(tzinfo=now.tzinfo) - now).total_seconds() / 3600
        current[hour] = blended_rain_pct(row["rain_pct"], lead)
    if replan._last_rain and not routing.rain_changed(replan._last_rain, current):
        log.info("plan.commute · forecast steady; no re-solve")
        return _disabled_summary()
    return await _solve_for_commute(session, "weather", account_key)


async def cleanup_stray_commute_legs(
    session: Session,
    *,
    account_key: str | None = None,
) -> int:
    if not get_settings().commute_enabled:
        return 0
    from app.services.commute.planner import delete_stray_commute_legs
    from app.services.plan.schedule import _schedule_lock

    async with _schedule_lock:
        return await delete_stray_commute_legs(session, account_key=account_key)


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
        summary = await plan_commutes_window(
            session,
            window_start=window_start,
            window_end=window_end,
            account_key=account_key,
            _depth=_depth,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.commute · window planning failed err=%s", exc)
        return
    # Best-effort means don't propagate — not don't tell anyone. A setup
    # error here (missing Maps key, no home address) is the only trace of
    # why an edited event never received its legs.
    if summary["errors"]:
        log.warning("plan.commute · window planning errors · %s", summary["errors"])
    log.info(
        "plan.commute · window %s..%s · legs=%d rescheduled_tasks=%d unroutable=%d",
        window_start.isoformat(), window_end.isoformat(),
        summary["planned"], summary["rescheduled_tasks"], summary["skipped_unroutable"],
    )
