"""Google Maps Distance Matrix client.

Only one endpoint is used; the wrapper exists so the planner can stay
sync-style (cache hit → no I/O) while still doing async HTTP on cache miss.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

TravelMode = Literal["bicycling", "transit", "driving", "walking"]

_BASE = "https://maps.googleapis.com/maps/api/distancematrix/json"
_TIMEOUT = 10.0


class MapsLookupError(RuntimeError):
    pass


async def distance(
    *,
    origin: str,
    destination: str,
    mode: TravelMode,
    departure: datetime | None = None,
) -> tuple[int, int | None]:
    """Return `(duration_seconds, distance_meters)` for the trip.

    `departure` is forwarded as a unix timestamp for transit mode (so Google
    snaps to the next departure) and as `now` for driving (live traffic). For
    bicycling / walking the timestamp is ignored by Google but we still pass
    it so call signatures stay uniform.
    """
    s = get_settings()
    if not s.google_maps_api_key:
        raise MapsLookupError("GOOGLE_MAPS_API_KEY is not configured")

    params: dict[str, str] = {
        "origins": origin,
        "destinations": destination,
        "mode": mode,
        "key": s.google_maps_api_key,
        "units": "metric",
    }
    if mode in ("transit", "driving") and departure is not None:
        params["departure_time"] = str(int(departure.timestamp()))

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_BASE, params=params)
        resp.raise_for_status()
        payload = resp.json()

    if payload.get("status") != "OK":
        raise MapsLookupError(
            f"distance matrix status={payload.get('status')} "
            f"msg={payload.get('error_message')!r}"
        )
    try:
        element = payload["rows"][0]["elements"][0]
    except (IndexError, KeyError) as exc:
        raise MapsLookupError(f"unexpected distance matrix shape: {payload}") from exc

    if element.get("status") != "OK":
        # `ZERO_RESULTS` is the common case here — e.g. no transit at 03:00.
        raise MapsLookupError(f"element status={element.get('status')}")

    duration_s = int(element["duration"]["value"])
    distance_m = element.get("distance", {}).get("value")
    return duration_s, int(distance_m) if distance_m is not None else None
