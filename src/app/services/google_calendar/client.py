"""HTTP client + normalized event shape for Google Calendar v3.

`GoogleCalendarClient` is a thin async wrapper around the three endpoints we
need; `CalendarEvent` is the normalized shape callers see (timezone-aware
datetimes, all-day flag, original raw payload preserved).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

_BASE = "https://www.googleapis.com/calendar/v3"


@dataclass
class CalendarEvent:
    """Normalized event shape across `events.list` and `events.insert`.

    `start` / `end` are always timezone-aware. All-day events come back from
    Google with date-only fields; we surface them as midnight-UTC datetimes
    and flag `all_day=True` so callers can distinguish.
    """

    id: str
    calendar_id: str
    summary: str
    description: str | None
    start: datetime
    end: datetime
    all_day: bool
    location: str | None
    html_link: str | None
    raw: dict[str, Any]


class GoogleCalendarClient:
    """Thin async wrapper around the three Calendar v3 endpoints we use."""

    def __init__(self, access_token: str, *, timeout: float = 15.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def list_events(
        self,
        calendar_id: str,
        *,
        time_min: datetime,
        time_max: datetime,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "timeMin": rfc3339(time_min),
            "timeMax": rfc3339(time_max),
            # Expand recurring events so callers see the actual instances in
            # the window rather than the master rule.
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            while True:
                resp = await client.get(
                    f"{_BASE}/calendars/{calendar_id}/events", params=params,
                )
                resp.raise_for_status()
                payload = resp.json()
                out.extend(payload.get("items", []))
                token = payload.get("nextPageToken")
                if not token:
                    return out
                params["pageToken"] = token

    async def insert_event(self, calendar_id: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.post(
                f"{_BASE}/calendars/{calendar_id}/events", json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def patch_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.patch(
                f"{_BASE}/calendars/{calendar_id}/events/{event_id}", json=body,
            )
            resp.raise_for_status()
            return resp.json()

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.delete(
                f"{_BASE}/calendars/{calendar_id}/events/{event_id}",
            )
            # 410 Gone = already deleted, treat as success.
            if resp.status_code == 410:
                return
            resp.raise_for_status()


def rfc3339(dt: datetime) -> str:
    return dt.isoformat()


def tz_name(dt: datetime) -> str:
    # Calendar accepts either an IANA tz name (`Europe/Berlin`) or a UTC
    # offset string; both work as long as it matches the `dateTime` offset.
    name = getattr(dt.tzinfo, "key", None)
    if isinstance(name, str):
        return name
    return str(dt.tzinfo)


def normalize(item: dict, calendar_id: str) -> CalendarEvent:
    start, all_day_s = _parse_time(item.get("start") or {})
    end, all_day_e = _parse_time(item.get("end") or {})
    return CalendarEvent(
        id=item["id"],
        calendar_id=calendar_id,
        summary=item.get("summary") or "(untitled)",
        description=item.get("description"),
        start=start,
        end=end,
        all_day=all_day_s and all_day_e,
        location=item.get("location"),
        html_link=item.get("htmlLink"),
        raw=item,
    )


def _parse_time(payload: dict) -> tuple[datetime, bool]:
    if "dateTime" in payload:
        return datetime.fromisoformat(payload["dateTime"].replace("Z", "+00:00")), False
    # All-day event: `date` is YYYY-MM-DD with no time component.
    return datetime.fromisoformat(payload["date"]).replace(tzinfo=timezone.utc), True
