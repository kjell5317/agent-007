"""Cached route-duration resolver for commute planning."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import route_cache
from app.services.commute.client import MapsLookupError, TravelMode, distance
from app.timezones import to_user_tz

log = logging.getLogger(__name__)


async def resolve_duration(
    session: Session,
    *,
    origin: str,
    destination: str,
    mode: TravelMode,
    departure: datetime | None = None,
) -> int | None:
    bucket = _hour_bucket(departure)
    cached = route_cache.lookup(
        session,
        origin=origin,
        destination=destination,
        mode=mode,
        hour_bucket=bucket,
    )
    if cached is not None:
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


def _hour_bucket(departure: datetime | None) -> int:
    local = to_user_tz(departure or datetime.now(timezone.utc))
    return local.weekday() * 24 + local.hour
