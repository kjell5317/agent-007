"""Cached duration lookup.

`resolve_duration(...)` is the single entry point — it hits `route_cache`
first, then falls back to a live Distance Matrix call and writes the result
back. The planner only ever calls this function; the maps client is an
implementation detail.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.services.commute import maps_client
from app.services.commute.maps_client import MapsLookupError, TravelMode
from app.storage import route_cache as route_cache_store

log = logging.getLogger(__name__)


def hour_bucket(when: datetime) -> int:
    """`weekday * 24 + hour` in local time. Range 0..167."""
    local = when.astimezone()
    return local.weekday() * 24 + local.hour


async def resolve_duration(
    session: Session,
    *,
    origin: str,
    destination: str,
    mode: TravelMode,
    departure: datetime,
) -> int | None:
    """Duration in seconds for the trip. `None` if Google can't route it."""
    bucket = hour_bucket(departure)
    cached = route_cache_store.lookup(
        session,
        origin=origin,
        destination=destination,
        mode=mode,
        hour_bucket=bucket,
    )
    if cached is not None:
        log.debug(
            "route cache hit · %s -> %s mode=%s bucket=%d → %ds",
            origin, destination, mode, bucket, cached.duration_seconds,
        )
        return cached.duration_seconds

    try:
        duration_s, distance_m = await maps_client.distance(
            origin=origin,
            destination=destination,
            mode=mode,
            departure=departure,
        )
    except MapsLookupError as exc:
        log.info(
            "maps lookup failed · %s -> %s mode=%s err=%s",
            origin, destination, mode, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — network/HTTP errors
        log.warning(
            "maps lookup exception · %s -> %s mode=%s err=%s",
            origin, destination, mode, exc,
        )
        return None

    route_cache_store.upsert(
        session,
        origin=origin,
        destination=destination,
        mode=mode,
        hour_bucket=bucket,
        duration_seconds=duration_s,
        distance_meters=distance_m,
    )
    session.commit()
    log.info(
        "route cache fill · %s -> %s mode=%s bucket=%d → %ds",
        origin, destination, mode, bucket, duration_s,
    )
    return duration_s
