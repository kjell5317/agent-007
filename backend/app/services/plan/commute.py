"""Commute entry points outside a task placement: a fresh solve when
something the legs depend on changed, the hourly weather check, and the
sweep of legs outside the live window. The legs themselves come out of the
solve (`app.services.plan.routing`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings

log = logging.getLogger(__name__)


async def replan_commutes(session: Session, *, account_key: str | None = None) -> None:
    """Best effort: a failed solve is logged, never raised — the next trigger
    or the nightly solve rewrites the legs."""
    from app.services.plan.replan import request_replan
    from app.services.plan.routing import routing_enabled

    if not routing_enabled():
        return
    try:
        result = await request_replan(session, reason="commute_window", account_key=account_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.commute · replan failed err=%s", exc)
        return
    log.info("plan.commute · replan legs=%d", len(result.routes or []))


async def refresh_commutes_for_weather(
    session: Session,
    *,
    account_key: str | None = None,
) -> int | None:
    """Re-solve only when a rain probability for the next 48 h moved by 10
    points or more since the last applied solve. Returns the legs that solve
    planned, None when none ran."""
    from app.services.commute.weather import blended_rain_pct, geocode, hourly_forecast
    from app.services.plan import replan, routing
    from app.timezones import user_tz

    if not routing.routing_enabled():
        return None
    now = datetime.now(user_tz())
    latlon = await geocode(session, get_settings().home_address)
    if latlon is None:
        return None
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
        return None
    result = await replan.request_replan(session, reason="weather", account_key=account_key)
    return len(result.routes or [])


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
