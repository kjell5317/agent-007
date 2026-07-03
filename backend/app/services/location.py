"""Shared location normalization helpers."""

from __future__ import annotations

import logging
import re

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_HOME_ALIASES = frozenset({"home"})

_ONLINE_DOMAINS = (
    "zoom.us",
    "meet.google",
    "teams.microsoft",
    "webex.",
    "meet.jit.si",
    "discord.",
    "skype.",
)
_ONLINE_WORDS = re.compile(
    r"\b(online|remote|virtual|webinar|zoom|teams|google meet|ms teams|"
    r"microsoft teams|video ?call|phone ?call)\b",
    re.IGNORECASE,
)


def resolve_location_alias(location: str | None) -> str | None:
    """Resolve user-facing location aliases into routable addresses."""
    if location is None:
        return None
    clean = location.strip()
    if not clean:
        return None
    if clean.lower() in _HOME_ALIASES:
        home = (get_settings().home_address or "").strip()
        return home or clean
    return clean


# TUMonline room key as it appears (parenthesized) in calendar locations,
# e.g. "00.5901.051, Hörsaal (5901.EG.051)" — building.floor.number.
_TUM_ROOM_RE = re.compile(r"\b(\d{4}\.[A-Z0-9]{1,3}\.\d{3,4}[A-Z]?)\b")

# nav.tum.de responses are effectively static; cache for the process lifetime.
# Only definitive answers are cached — transient failures retry next replan.
_tum_room_cache: dict[str, str | None] = {}


def tum_room_id(location: str | None) -> str | None:
    match = _TUM_ROOM_RE.search(location or "")
    return match.group(1) if match else None


async def resolve_tum_room(location: str | None) -> str | None:
    """Routable address for a TUMonline room location via nav.tum.de —
    Google Maps can't geocode room strings like "00.5901.051, Hörsaal
    (5901.EG.051)". Returns None when the location isn't a TUM room or
    the lookup fails."""
    room = tum_room_id(location)
    if room is None:
        return None
    if room in _tum_room_cache:
        return _tum_room_cache[room]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"https://nav.tum.de/api/locations/{room}")
            if resp.status_code == 404:
                # Definitive miss — remember it so replans don't re-ask.
                _tum_room_cache[room] = None
                return None
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.info("nav.tum.de lookup failed for %r (%s)", room, exc)
        return None
    resolved = _nav_tum_address(payload)
    _tum_room_cache[room] = resolved
    return resolved


def _nav_tum_address(payload: dict) -> str | None:
    computed = (payload.get("props") or {}).get("computed") or []
    for prop in computed:
        if prop.get("name") in ("Adresse", "Address") and prop.get("text"):
            return str(prop["text"])
    coords = payload.get("coords") or {}
    lat, lon = coords.get("lat"), coords.get("lon")
    if lat is not None and lon is not None:
        return f"{lat},{lon}"
    return None


def is_online_location(location: str | None) -> bool:
    """Whether a location string denotes a meeting link / virtual place
    rather than somewhere routable."""
    clean = (location or "").strip()
    if not clean:
        return True
    lowered = clean.lower()
    if lowered.startswith(("http://", "https://")):
        return True
    if any(domain in lowered for domain in _ONLINE_DOMAINS):
        return True
    return _ONLINE_WORDS.search(clean) is not None
