"""Shared location normalization helpers."""

from __future__ import annotations

import re

from app.config import get_settings

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
