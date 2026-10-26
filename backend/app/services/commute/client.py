"""Google Maps clients: Distance Matrix for the travel durations the
planner models with, the Routes API for the real transit connection a
written leg takes. The wrappers exist so the planner can stay sync-style
(cache hit → no I/O) while still doing async HTTP on cache miss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

TravelMode = Literal["bicycling", "transit", "driving", "walking"]

_BASE = "https://maps.googleapis.com/maps/api/distancematrix/json"
_ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
_ROUTES_FIELDS = "routes.legs.steps.staticDuration,routes.legs.steps.transitDetails"
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


@dataclass(frozen=True)
class TransitConnection:
    depart: datetime  # leaving the door, the walk to the stop included
    arrive: datetime
    boards: datetime
    line: str | None
    stop: str | None


async def transit_connection(
    *,
    origin: str,
    destination: str,
    depart_at: datetime | None = None,
    arrive_by: datetime | None = None,
) -> TransitConnection | None:
    """The first connection leaving at `depart_at`, or the last one arriving
    by `arrive_by`. None when no transit runs (walking only, or no route)."""
    s = get_settings()
    if not s.google_maps_api_key:
        raise MapsLookupError("GOOGLE_MAPS_API_KEY is not configured")
    body: dict[str, Any] = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": "TRANSIT",
    }
    if arrive_by is not None:
        body["arrivalTime"] = _rfc3339(arrive_by)
    elif depart_at is not None:
        body["departureTime"] = _rfc3339(depart_at)
    headers = {"X-Goog-Api-Key": s.google_maps_api_key, "X-Goog-FieldMask": _ROUTES_FIELDS}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(_ROUTES, json=body, headers=headers)
    if resp.status_code != 200:
        raise MapsLookupError(f"routes status={resp.status_code} body={resp.text[:300]!r}")
    return parse_transit_route(resp.json())


def parse_transit_route(payload: dict) -> TransitConnection | None:
    """Door-to-door times: the first ride's departure minus the walk before
    it, the last ride's arrival plus the walk after it."""
    routes = payload.get("routes") or []
    if not routes:
        return None
    steps = [step for leg in routes[0].get("legs", []) for step in leg.get("steps", [])]
    rides = [i for i, step in enumerate(steps) if step.get("transitDetails")]
    if not rides:
        return None
    first = steps[rides[0]]["transitDetails"]
    last = steps[rides[-1]]["transitDetails"]
    try:
        boards = datetime.fromisoformat(first["stopDetails"]["departureTime"])
        alights = datetime.fromisoformat(last["stopDetails"]["arrivalTime"])
    except (KeyError, ValueError):
        # Frequency-based lines can come without a timetable.
        return None
    walk_before = sum(_seconds(step.get("staticDuration")) for step in steps[: rides[0]])
    walk_after = sum(_seconds(step.get("staticDuration")) for step in steps[rides[-1] + 1 :])
    line = first.get("transitLine") or {}
    return TransitConnection(
        depart=boards - timedelta(seconds=walk_before),
        arrive=alights + timedelta(seconds=walk_after),
        boards=boards,
        line=line.get("nameShort") or line.get("name"),
        stop=(first["stopDetails"].get("departureStop") or {}).get("name"),
    )


def _seconds(duration: str | None) -> float:
    return float(duration.rstrip("s")) if duration else 0.0


def _rfc3339(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
