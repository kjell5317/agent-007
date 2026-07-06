"""HTTP client for Google Health sleep data."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.auth.google_tokens import (
    GoogleReauthorizationRequired,
    GoogleTokenMissing,
    get_fresh_google_token,
)

log = logging.getLogger(__name__)

_BASE = "https://health.googleapis.com/v4"
_SLEEP_PARENT = "users/me/dataTypes/sleep"
# The sleep list method caps pageSize at 25; a single local day is far under
# that, so we never need to page.
_SLEEP_PAGE_SIZE = 25


class GoogleHealthClient:
    """Thin async wrapper around the Google Health REST endpoints we need."""

    def __init__(self, access_token: str, *, timeout: float = 15.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def list_sleep(self, *, start: datetime, end: datetime) -> dict[str, Any]:
        """List sleep data points whose interval ends in `[start, end)`."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if end <= start:
            raise ValueError("end must be after start")

        params = {
            "filter": (
                f'sleep.interval.end_time >= "{_rfc3339(start)}" AND '
                f'sleep.interval.end_time < "{_rfc3339(end)}"'
            ),
            "pageSize": _SLEEP_PAGE_SIZE,
        }
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.get(f"{_BASE}/{_SLEEP_PARENT}/dataPoints", params=params)
            if resp.is_error:
                # Google's error body carries the actual reason (SERVICE_DISABLED,
                # PERMISSION_DENIED, an activation link, …); surface it before raising.
                log.warning("google health sleep · %s %s", resp.status_code, resp.text)
                resp.raise_for_status()
            return resp.json()


async def authorized_client(session: Session, account_key: str | None) -> GoogleHealthClient:
    """Resolve the health-only Google token, refreshing on demand, and wrap it.

    Uses the `google_health` grant — a health-scoped token distinct from the
    Gmail/Calendar one, since the Health API rejects tokens with other scopes.
    """
    try:
        token = await get_fresh_google_token(
            session, account_key=account_key, provider="google_health"
        )
    except GoogleTokenMissing:
        raise RuntimeError(
            "No Google Health authorization — visit /oauth/google_health/authorize first."
        )
    except GoogleReauthorizationRequired:
        raise RuntimeError(
            "Google Health token expired and no refresh_token available; re-authorize "
            "at /oauth/google_health/authorize."
        )
    return GoogleHealthClient(token.access_token)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
