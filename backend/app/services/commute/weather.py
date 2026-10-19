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

from app.timezones import to_user_tz

log = logging.getLogger(__name__)

_BASE = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8.0


async def precipitation_probability_at(
    lat: float, lon: float, when: datetime
) -> int | None:
    """Probability of precipitation (%) at the given hour."""
    iso_hour = to_user_tz(when).strftime("%Y-%m-%dT%H:00")
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
    start_hour = to_user_tz(start).strftime("%Y-%m-%dT%H:00")
    end_hour = to_user_tz(end).strftime("%Y-%m-%dT%H:00")
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


async def geocode(session, address: str) -> tuple[float, float] | None:
    """Resolve `address` to `(lat, lon)` via Google's free-tier geocoding.

    The Distance Matrix API geocodes implicitly on each call, but we need a
    separate coordinate to query the weather. We use the same Maps key.
    Results are cached in the DB forever — coordinates of an address
    effectively never change.
    """
    from app.config import get_settings
    from app.db.clients import geocode_cache

    cached = geocode_cache.lookup(session, address=address)
    if cached is not None:
        return cached

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
        # REQUEST_DENIED here usually means the key lacks the Geocoding API
        # (Distance Matrix being enabled is not enough); ZERO_RESULTS means
        # the address itself doesn't resolve.
        log.warning(
            "geocode · no result for %r status=%s msg=%r",
            address, payload.get("status"), payload.get("error_message"),
        )
        return None
    loc = results[0].get("geometry", {}).get("location") or {}
    if "lat" not in loc or "lng" not in loc:
        return None
    lat, lon = float(loc["lat"]), float(loc["lng"])
    geocode_cache.upsert(session, address=address, lat=lat, lon=lon)
    session.commit()
    return lat, lon


# --- Hourly forecast for the solver ------------------------------------------

FORECAST_CACHE_SECONDS = 3600
# Long-run share of rainy hours, what a forecast decays to with lead time.
BASE_RAIN_PCT = 20.0
_forecast_cache: dict[tuple, tuple[float, dict]] = {}


async def hourly_forecast(
    lat: float, lon: float, start: datetime, end: datetime
) -> dict[str, dict[str, float]]:
    """`{"YYYY-MM-DDTHH:00": {rain_pct, rain_mm, temp_c, wind_kmh, is_day}}`
    in local time. Cached for an hour; an empty dict when unreachable."""
    import time

    start_hour = to_user_tz(start).strftime("%Y-%m-%dT%H:00")
    end_hour = to_user_tz(end).strftime("%Y-%m-%dT%H:00")
    key = (round(lat, 3), round(lon, 3), start_hour[:10], end_hour[:10])
    cached = _forecast_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < FORECAST_CACHE_SECONDS:
        return cached[1]
    params = {
        "latitude": f"{lat:.5f}",
        "longitude": f"{lon:.5f}",
        "hourly": "precipitation_probability,precipitation,temperature_2m,wind_speed_10m,is_day",
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
        log.info("open-meteo forecast failed (%s) — no weather signal", exc)
        return {}
    out = parse_hourly(payload.get("hourly") or {})
    _forecast_cache[key] = (time.monotonic(), out)
    return out


def parse_hourly(hourly: dict) -> dict[str, dict[str, float]]:
    fields = {
        "rain_pct": "precipitation_probability",
        "rain_mm": "precipitation",
        "temp_c": "temperature_2m",
        "wind_kmh": "wind_speed_10m",
        "is_day": "is_day",
    }
    times = hourly.get("time") or []
    out: dict[str, dict[str, float]] = {}
    for n, when in enumerate(times):
        row = {}
        for name, source in fields.items():
            series = hourly.get(source) or []
            if n < len(series) and series[n] is not None:
                row[name] = float(series[n])
        out[str(when)] = row
    return out


def blended_rain_pct(pct: float, lead_hours: float) -> float:
    """Forecast skill fades with lead time: trust it fully for a day, then
    blend toward the base rate (weight 0.3 at a week)."""
    if lead_hours <= 24:
        return pct
    weight = max(0.3, 1 - 0.7 * (lead_hours - 24) / (7 * 24 - 24))
    return weight * pct + (1 - weight) * BASE_RAIN_PCT
