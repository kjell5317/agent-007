"""Shared location normalization helpers."""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

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
_COORD_RE = re.compile(
    r"(?<![\d.-])(?P<lat>-?\d+(?:\.\d+)?),(?P<lng>-?\d+(?:\.\d+)?)(?![\d.-])"
)
_MAPS_AT_COORD_RE = re.compile(
    r"/@(?P<lat>-?\d+(?:\.\d+)?),(?P<lng>-?\d+(?:\.\d+)?)(?:[,/]|$)"
)
_GOOGLE_MAPS_REDIRECT_TIMEOUT = 3.0


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


def is_google_maps_url(location: str | None) -> bool:
    clean = (location or "").strip()
    if not clean.lower().startswith(("http://", "https://")):
        return False
    parsed = urlparse(clean)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host == "maps.app.goo.gl":
        return True
    if host == "goo.gl" and path.startswith("/maps"):
        return True
    if host.startswith("maps.google."):
        return True
    if _is_google_host(host) and path.startswith("/maps"):
        return True
    return False


def resolve_google_maps_url_sync(location: str | None) -> str | None:
    """Parse a direct Google Maps URL into a Distance Matrix-friendly value.

    This intentionally does not follow redirects, so callers that must stay
    cache-only can use it without hidden network I/O.
    """
    clean = (location or "").strip()
    if not is_google_maps_url(clean):
        return None
    return _routable_from_google_maps_url(clean)


async def resolve_google_maps_url(location: str | None) -> str | None:
    """Resolve a Google Maps URL into an address/search string or lat,lng."""
    clean = (location or "").strip()
    if not is_google_maps_url(clean):
        return None
    direct = _routable_from_google_maps_url(clean)
    if direct is not None:
        return direct
    if not _is_short_google_maps_url(clean):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=_GOOGLE_MAPS_REDIRECT_TIMEOUT,
            follow_redirects=True,
        ) as client:
            resp = await client.get(clean)
            hops = [str(r.url) for r in (*getattr(resp, "history", []), resp)]
    except Exception as exc:  # noqa: BLE001
        log.info("Google Maps short-link expansion failed for %r (%s)", clean, exc)
        return None
    # The chain often ends on consent.google.com (EU cookie interstitial)
    # with the real maps URL in its `continue` param, while an intermediate
    # hop already carried the coordinates — walk it most-resolved-first.
    for url in reversed(hops):
        if url == clean:
            continue
        routable = _routable_from_google_maps_url(_unwrap_consent_url(url))
        if routable is not None:
            return routable
    log.info("Google Maps short-link %r resolved to nothing routable (%s)", clean, hops[-1])
    return None


def _unwrap_consent_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != "consent.google.com":
        return url
    for value in parse_qs(parsed.query).get("continue", []):
        return value
    return url


def resolve_routable_location_sync(location: str | None) -> str | None:
    """Cache-only physical-location normalization.

    Google Maps URLs are used only when their routable value is available
    without redirects; short links return None instead of blocking on I/O.
    """
    clean = resolve_location_alias(location)
    if not clean or is_online_location(clean):
        return None
    if is_google_maps_url(clean):
        return resolve_google_maps_url_sync(clean)
    if tum_room_id(clean):
        return None
    return clean


async def resolve_routable_location(location: str | None) -> str | None:
    """Normalize a user/calendar location into a routable physical target."""
    clean = resolve_location_alias(location)
    if not clean or is_online_location(clean):
        return None
    if is_google_maps_url(clean):
        resolved = await resolve_google_maps_url(clean)
        return resolved or clean
    resolved_tum = await resolve_tum_room(clean)
    return resolved_tum or clean


def _is_google_host(host: str) -> bool:
    return bool(re.fullmatch(r"(?:www\.)?google\.[a-z.]+", host))


def _is_short_google_maps_url(location: str) -> bool:
    parsed = urlparse(location)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    return host == "maps.app.goo.gl" or (host == "goo.gl" and path.startswith("/maps"))


def _routable_from_google_maps_url(location: str) -> str | None:
    parsed = urlparse(location)
    path = unquote(parsed.path or "")
    at_coords = _MAPS_AT_COORD_RE.search(path)
    if at_coords and _valid_lat_lng(at_coords.group("lat"), at_coords.group("lng")):
        return f"{at_coords.group('lat')},{at_coords.group('lng')}"

    query = parse_qs(parsed.query)
    for key in ("q", "query", "destination"):
        for value in query.get(key, []):
            clean = _clean_maps_value(value)
            coords = _coordinates_from_text(clean)
            if coords is not None:
                return coords
    for key in ("q", "query", "destination"):
        for value in query.get(key, []):
            clean = _clean_maps_value(value)
            if clean:
                return clean

    place = _place_from_path(path)
    if place:
        return place
    return None


def _coordinates_from_text(text: str) -> str | None:
    match = _COORD_RE.search(text)
    if not match:
        return None
    lat, lng = match.group("lat"), match.group("lng")
    if not _valid_lat_lng(lat, lng):
        return None
    return f"{lat},{lng}"


def _valid_lat_lng(lat: str, lng: str) -> bool:
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except ValueError:
        return False
    return -90 <= lat_f <= 90 and -180 <= lng_f <= 180


def _place_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    for idx, part in enumerate(parts):
        if part == "place" and idx + 1 < len(parts):
            return _clean_maps_value(parts[idx + 1])
    return None


def _clean_maps_value(value: str) -> str:
    return " ".join(unquote(value).replace("+", " ").split())


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
        return not is_google_maps_url(clean)
    if any(domain in lowered for domain in _ONLINE_DOMAINS):
        return True
    return _ONLINE_WORDS.search(clean) is not None
