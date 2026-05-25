"""Cached duration lookup.

`resolve_duration(...)` is the single entry point — it hits `route_cache`
first, then falls back to a live Distance Matrix call and writes the result
back. The planner only ever calls this function; the maps client is an
implementation detail.

Two caching quirks worth knowing:

  * **Bike** — duration is independent of departure hour (Maps doesn't model
    cycling rush hour). We collapse every hour bucket into `BIKE_BUCKET = 0`,
    so a bike route ever costs at most one Distance Matrix call regardless
    of how many event hours hit it.
  * **Negative cache** — `ZERO_RESULTS` / `NOT_FOUND` from Maps are stable:
    retrying won't help. We persist them as `duration_seconds = -1` so the
    same bad address can't drain quota one call at a time.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.services.commute import maps_client
from app.services.commute.maps_client import MapsLookupError, TravelMode
from app.storage import route_cache as route_cache_store

log = logging.getLogger(__name__)

BIKE_BUCKET = 0
NEGATIVE_CACHE_SENTINEL = -1


def hour_bucket(when: datetime, mode: TravelMode) -> int:
    """`weekday * 24 + hour` in local time. Range 0..167.

    Bike collapses to a single bucket because cycling time doesn't vary with
    departure hour on Distance Matrix."""
    if mode == "bicycling":
        return BIKE_BUCKET
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
    """Duration in seconds for the trip. `None` if Google can't route it.

    A `None` return from a cache hit is permanent (negative cache); a `None`
    from a transient failure is not persisted, so the next call will retry.
    """
    bucket = hour_bucket(departure, mode)
    cached = route_cache_store.lookup(
        session,
        origin=origin,
        destination=destination,
        mode=mode,
        hour_bucket=bucket,
    )
    if cached is not None:
        if cached.duration_seconds == NEGATIVE_CACHE_SENTINEL:
            log.debug(
                "route cache hit (negative) · %s -> %s mode=%s",
                origin, destination, mode,
            )
            return None
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
        if exc.cacheable:
            route_cache_store.upsert(
                session,
                origin=origin,
                destination=destination,
                mode=mode,
                hour_bucket=bucket,
                duration_seconds=NEGATIVE_CACHE_SENTINEL,
                distance_meters=None,
            )
            session.commit()
            log.info(
                "route cache fill (negative) · %s -> %s mode=%s err=%s",
                origin, destination, mode, exc,
            )
        else:
            log.info(
                "maps lookup failed (transient) · %s -> %s mode=%s err=%s",
                origin, destination, mode, exc,
            )
        return None
    except Exception as exc:  # noqa: BLE001 — network/HTTP errors are transient
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
