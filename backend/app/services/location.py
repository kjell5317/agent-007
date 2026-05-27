"""Shared location normalization helpers."""

from __future__ import annotations

from app.config import get_settings

_HOME_ALIASES = frozenset({"home"})


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
