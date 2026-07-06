"""HTTP client for Google Health sleep data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.auth.google_tokens import (
    GoogleReauthorizationRequired,
    GoogleTokenMissing,
    get_fresh_google_token,
)

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
            resp.raise_for_status()
            return resp.json()


async def authorized_client(session: Session, account_key: str | None) -> GoogleHealthClient:
    """Resolve a Google token, refreshing it on demand, and wrap it for use."""
    try:
        token = await get_fresh_google_token(session, account_key=account_key)
    except GoogleTokenMissing:
        raise RuntimeError(
            "No Google account connected — sign in via /auth/login first."
        )
    except GoogleReauthorizationRequired:
        raise RuntimeError(
            "Google access token expired and no refresh_token available; re-authorize."
        )
    return GoogleHealthClient(token.access_token)


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
