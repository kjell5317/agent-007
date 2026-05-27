"""User-timezone resolver shared by planner, notifier and any other layer
that needs to render wall-clock time.

`datetime.astimezone()` with no argument uses the *process*'s local zone —
UTC inside most Docker images. Anywhere we want the user's wall-clock,
import `to_user_tz` (or `user_tz`) instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_settings

log = logging.getLogger(__name__)


def user_tz() -> ZoneInfo | timezone:
    name = (get_settings().user_timezone or "UTC").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("user_timezone=%r not found; falling back to UTC", name)
        return timezone.utc


def to_user_tz(dt: datetime) -> datetime:
    """Convert `dt` into the configured user timezone. Naive inputs are
    treated as UTC to match how Postgres returns timestamps."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(user_tz())
