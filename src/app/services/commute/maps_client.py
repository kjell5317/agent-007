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
    """`cacheable=True` means the result won't change on retry (bad address,
    no route exists, route too long) — callers should persist the failure so
    they don't burn API calls re-asking. `cacheable=False` covers transient
    failures: rate limits, network blips, malformed requests."""

    def __init__(self, message: str, *, cacheable: bool = False) -> None:
        super().__init__(message)
        self.cacheable = cacheable


# Element-level statuses Google returns that won't change on retry. Anything
# else (OVER_QUERY_LIMIT, REQUEST_DENIED, …) is transient — don't poison the
# cache with it.
_CACHEABLE_FAILURE_STATUSES = frozenset(
    {"ZERO_RESULTS", "NOT_FOUND", "MAX_ROUTE_LENGTH_EXCEEDED"}
)


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

    top_status = payload.get("status")
    if top_status != "OK":
        # Top-level non-OK is config / quota — never poison the cache from here.
        raise MapsLookupError(
            f"distance matrix status={top_status} "
            f"msg={payload.get('error_message')!r}",
            cacheable=False,
        )
    try:
        element = payload["rows"][0]["elements"][0]
    except (IndexError, KeyError) as exc:
        raise MapsLookupError(
            f"unexpected distance matrix shape: {payload}", cacheable=False,
        ) from exc

    element_status = element.get("status")
    if element_status != "OK":
        # `ZERO_RESULTS` is the common case here — e.g. no transit at 03:00.
        raise MapsLookupError(
            f"element status={element_status}",
            cacheable=element_status in _CACHEABLE_FAILURE_STATUSES,
        )

    duration_s = int(element["duration"]["value"])
    distance_m = element.get("distance", {}).get("value")
    return duration_s, int(distance_m) if distance_m is not None else None
