"""Open-Meteo precipitation probability lookup.

Free, no key required, ~6h cache resolution is plenty for "should I bike?".
We geocode the origin/destination through Google's Distance Matrix already, so
here we just take a precomputed `(lat, lon)` and read the forecast for the
relevant hour.

When the network call fails we return `None` — callers treat that as
"no rain signal", which keeps bike trips on by default rather than spending
extra API quota on a transit query.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8.0


async def precipitation_probability_at(
    lat: float, lon: float, when: datetime
) -> int | None:
    """Probability of precipitation (%) at the given hour."""
    when_local = when.astimezone()
    iso_hour = when_local.strftime("%Y-%m-%dT%H:00")
    params = {
        "latitude": f"{lat:.5f}",
        "longitude": f"{lon:.5f}",
        "hourly": "precipitation_probability",
        "timezone": "auto",
        "start_hour": iso_hour,
        "end_hour": iso_hour,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_BASE, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — best-effort weather check
        log.info("open-meteo lookup failed (%s) — assuming no rain signal", exc)
        return None

    series = payload.get("hourly", {}).get("precipitation_probability") or []
    if not series:
        return None
    value = series[0]
    return int(value) if value is not None else None


async def max_precipitation_probability_between(
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
) -> int | None:
    """Maximum precipitation probability (%) in `[start, end]`."""
    hourly = await precipitation_probabilities_between(lat, lon, start, end)
    return max(hourly.values()) if hourly else None


async def precipitation_probabilities_between(
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
) -> dict[str, int]:
    """Hourly precipitation probabilities keyed as local `YYYY-MM-DDTHH:00`."""
    start_hour = start.astimezone().strftime("%Y-%m-%dT%H:00")
    end_hour = end.astimezone().strftime("%Y-%m-%dT%H:00")
    params = {
        "latitude": f"{lat:.5f}",
        "longitude": f"{lon:.5f}",
        "hourly": "precipitation_probability",
        "timezone": "auto",
        "start_hour": start_hour,
        "end_hour": end_hour,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_BASE, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.info("open-meteo range lookup failed (%s) — no weather signal", exc)
        return {}

    hourly = payload.get("hourly", {})
    times = hourly.get("time") or []
    series = hourly.get("precipitation_probability") or []
    out: dict[str, int] = {}
    for when, value in zip(times, series, strict=False):
        if value is not None:
            out[str(when)] = int(value)
    return out


async def geocode(address: str) -> tuple[float, float] | None:
    """Resolve `address` to `(lat, lon)` via Google's free-tier geocoding.

    The Distance Matrix API geocodes implicitly on each call, but we need a
    separate coordinate to query the weather. We use the same Maps key.
    """
    from app.config import get_settings

    s = get_settings()
    if not s.google_maps_api_key:
        return None
    params = {"address": address, "key": s.google_maps_api_key}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                "https://maps.googleapis.com/maps/api/geocode/json", params=params,
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.info("geocode failed for %r (%s)", address, exc)
        return None

    results = payload.get("results") or []
    if not results:
        return None
    loc = results[0].get("geometry", {}).get("location") or {}
    if "lat" not in loc or "lng" not in loc:
        return None
    return float(loc["lat"]), float(loc["lng"])
