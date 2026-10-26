"""Open-Meteo hourly forecast for the solver's ride-weather terms, plus the
geocoding of the home address it is read at.

Free, no key required. When the network call fails the forecast is empty —
callers treat that as "no weather signal", which keeps bike trips on by
default.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.timezones import to_user_tz

log = logging.getLogger(__name__)

_BASE = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8.0


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
