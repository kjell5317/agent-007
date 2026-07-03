"""Cached route-duration resolver for commute planning.

Bike and walking durations don't vary with time of day (Google ignores the
departure time for them), so they share a single cache bucket per route.
Transit and driving stay bucketed by hour-of-week, and their cached values
expire after `commute_transit_ttl_days` so timetable/traffic changes are
eventually picked up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import route_cache
from app.services.commute.client import MapsLookupError, TravelMode, distance
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

_TIME_INVARIANT_MODES = frozenset({"bicycling", "walking"})


async def resolve_duration(
    session: Session,
    *,
    origin: str,
    destination: str,
    mode: TravelMode,
    departure: datetime | None = None,
) -> int | None:
    bucket = _hour_bucket(departure, mode)
    cached = route_cache.lookup_with_bicycling_reverse(
        session,
        origin=origin,
        destination=destination,
        mode=mode,
        hour_bucket=bucket,
    )
    if cached is not None and not _is_stale(cached, mode):
        return cached.duration_seconds

    try:
        duration_seconds, distance_meters = await distance(
            origin=origin,
            destination=destination,
            mode=mode,
            departure=departure,
        )
    except MapsLookupError as exc:
        if exc.cacheable:
            log.debug("route resolver · no route %s -> %s mode=%s", origin, destination, mode)
            return None
        if cached is not None:
            # Transient failure with an expired entry in hand — stale beats
            # nothing, and the next successful call refreshes it.
            log.info(
                "route resolver · transient failure, serving stale %s -> %s mode=%s",
                origin, destination, mode,
            )
            return cached.duration_seconds
        raise

    route_cache.upsert(
        session,
        origin=origin,
        destination=destination,
        mode=mode,
        hour_bucket=bucket,
        duration_seconds=duration_seconds,
        distance_meters=distance_meters,
    )
    session.commit()
    return duration_seconds


def _hour_bucket(departure: datetime | None, mode: TravelMode) -> int:
    if mode in _TIME_INVARIANT_MODES:
        return 0
    local = to_user_tz(departure or datetime.now(timezone.utc))
    return local.weekday() * 24 + local.hour


def _is_stale(row, mode: TravelMode) -> bool:
    if mode in _TIME_INVARIANT_MODES:
        return False
    updated = row.updated_at
    if updated is None:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    ttl = timedelta(days=get_settings().commute_transit_ttl_days)
    return datetime.now(timezone.utc) - updated > ttl
