"""HTTP client for Google Fit sleep data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.auth.google_tokens import (
    GoogleReauthorizationRequired,
    GoogleTokenMissing,
    get_fresh_google_token,
)

_BASE = "https://www.googleapis.com/fitness/v1"
SLEEP_SEGMENT_DATA_TYPE = "com.google.sleep.segment"


class GoogleFitClient:
    """Thin async wrapper around the Google Fit REST endpoints we need."""

    def __init__(self, access_token: str, *, timeout: float = 15.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def aggregate_sleep_segments(self, *, start: datetime, end: datetime) -> dict[str, Any]:
        """Fetch sleep segment points in `[start, end)` via Google Fit aggregate."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if end <= start:
            raise ValueError("end must be after start")

        body = {
            "aggregateBy": [{"dataTypeName": SLEEP_SEGMENT_DATA_TYPE}],
            "startTimeMillis": _millis(start),
            "endTimeMillis": _millis(end),
        }
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.post(f"{_BASE}/users/me/dataset:aggregate", json=body)
            resp.raise_for_status()
            return resp.json()


async def authorized_client(session: Session, account_key: str | None) -> GoogleFitClient:
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
    return GoogleFitClient(token.access_token)


def _millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
