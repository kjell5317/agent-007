"""Google Calendar service.

Two operations the rest of the app cares about:

  * `list_week_events(...)` — fan out across one or more calendars and return
    every event in a 7-day window that starts at any timezone-aware datetime
    the caller passes. Shift the window by changing `week_start`.
  * `create_event(...)` — insert an event on a single target calendar.

Auth piggybacks on the same Google OAuth bundle the Gmail source uses; the
required scope (`calendar.events`) lives in `app.auth.google`. The caller
hands in a Session and (optionally) an `account_key`; this module handles
token refresh and Calendar API calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.auth import get_provider
from app.config import get_settings
from app.storage import oauth_tokens

log = logging.getLogger(__name__)

_BASE = "https://www.googleapis.com/calendar/v3"
WINDOW_DAYS = 7


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
    """Thin async wrapper around the two Calendar v3 endpoints we use."""

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
            "timeMin": _rfc3339(time_min),
            "timeMax": _rfc3339(time_max),
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


async def list_week_events(
    session: Session,
    *,
    calendar_ids: Iterable[str],
    week_start: datetime,
    account_key: str | None = None,
) -> list[CalendarEvent]:
    """Return every event in `[week_start, week_start + 7d)` across calendars.

    Pass any timezone-aware datetime as `week_start` — the window shifts with
    it (so "this week", "next week", or any arbitrary anchor are all just
    different `week_start` values).
    """
    if week_start.tzinfo is None:
        raise ValueError("week_start must be timezone-aware")

    ids = list(calendar_ids)
    if not ids:
        return []

    time_min = week_start
    time_max = week_start + timedelta(days=WINDOW_DAYS)

    client = await _authorized_client(session, account_key)
    events: list[CalendarEvent] = []
    for cid in ids:
        log.info(
            "calendar list · id=%s window=%s..%s",
            cid, time_min.isoformat(), time_max.isoformat(),
        )
        items = await client.list_events(cid, time_min=time_min, time_max=time_max)
        events.extend(_normalize(it, cid) for it in items)
    events.sort(key=lambda e: e.start)
    return events


async def create_event(
    session: Session,
    *,
    calendar_id: str,
    summary: str,
    start: datetime,
    end: datetime,
    description: str | None = None,
    location: str | None = None,
    account_key: str | None = None,
) -> CalendarEvent:
    """Create an event on `calendar_id`. `start`/`end` must be tz-aware."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")

    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": _rfc3339(start), "timeZone": _tz_name(start)},
        "end": {"dateTime": _rfc3339(end), "timeZone": _tz_name(end)},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    client = await _authorized_client(session, account_key)
    log.info("calendar insert · calendar=%s summary=%r", calendar_id, summary)
    raw = await client.insert_event(calendar_id, body)
    return _normalize(raw, calendar_id)


async def patch_event(
    session: Session,
    *,
    calendar_id: str,
    event_id: str,
    summary: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    description: str | None = None,
    location: str | None = None,
    account_key: str | None = None,
) -> CalendarEvent:
    """Patch an existing event on `calendar_id`. Only provided fields change."""
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        body["start"] = {"dateTime": _rfc3339(start), "timeZone": _tz_name(start)}
    if end is not None:
        if end.tzinfo is None:
            raise ValueError("end must be timezone-aware")
        body["end"] = {"dateTime": _rfc3339(end), "timeZone": _tz_name(end)}
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location

    client = await _authorized_client(session, account_key)
    log.info("calendar patch · calendar=%s event=%s", calendar_id, event_id)
    raw = await client.patch_event(calendar_id, event_id, body)
    return _normalize(raw, calendar_id)


async def add_task_to_calendar(session: Session, task) -> None:
    """Fire-and-forget: mirror `task` as a Google Calendar event.

    The event ends at `task.due_date` and starts `estimation` minutes earlier
    (falling back to `google_calendar_default_event_minutes` when estimation
    is missing). Skipped silently when due_date is None, when no Google
    account is connected, or when google_calendar_id is empty. Never raises
    — calendar failures must not break task creation. On success the new
    event id is persisted on `task.calendar_event_id` and committed.
    """
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    if task.due_date is None:
        log.debug("calendar sync · task=%s skipped (no due_date)", task.id)
        return

    start, end = _task_window(task, settings)
    try:
        event = await create_event(
            session,
            calendar_id=calendar_id,
            summary=task.title,
            start=start,
            end=end,
            description=_task_description(task),
            location=task.location,
        )
    except Exception as exc:  # noqa: BLE001 — never let calendar break task creation
        log.warning("calendar sync failed · task=%s err=%s", task.id, exc)
        return

    task.calendar_event_id = event.id
    session.commit()


async def update_task_in_calendar(session: Session, task) -> None:
    """Fire-and-forget: push the task's current fields to its calendar event.

    If the task has no `calendar_event_id` yet (e.g. it was created without a
    due_date and now has one), delegates to `add_task_to_calendar` so the
    event is created. Never raises.
    """
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return
    if task.due_date is None:
        log.debug("calendar sync · task=%s skipped (no due_date)", task.id)
        return
    if not task.calendar_event_id:
        await add_task_to_calendar(session, task)
        return

    start, end = _task_window(task, settings)
    try:
        await patch_event(
            session,
            calendar_id=calendar_id,
            event_id=task.calendar_event_id,
            summary=task.title,
            start=start,
            end=end,
            description=_task_description(task) or "",
            location=task.location or "",
        )
    except Exception as exc:  # noqa: BLE001 — never let calendar break task updates
        log.warning("calendar update failed · task=%s err=%s", task.id, exc)


def _task_window(task, settings) -> tuple[datetime, datetime]:
    end = task.due_date
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    minutes = task.estimation or settings.google_calendar_default_event_minutes
    return end - timedelta(minutes=minutes), end


def _task_description(task) -> str | None:
    parts: list[str] = []
    if task.description:
        parts.append(task.description)
    if task.link:
        parts.append(task.link)
    return "\n\n".join(parts) or None


# --- internals ----------------------------------------------------------------


async def _authorized_client(
    session: Session, account_key: str | None
) -> GoogleCalendarClient:
    """Resolve a Google token, refreshing it on demand, and wrap it for use."""
    token = oauth_tokens.get_decrypted(session, provider="google", account_key=account_key)
    if token is None:
        raise RuntimeError(
            "No Google account connected — sign in via /auth/login first."
        )
    if token.is_expired:
        if not token.refresh_token:
            raise RuntimeError(
                "Google access token expired and no refresh_token available; re-authorize."
            )
        bundle = await get_provider("google")().refresh(token.refresh_token)
        oauth_tokens.upsert(
            session,
            provider="google",
            account_key=token.account_key,
            bundle=bundle,
            extra_merge=token.extra,
        )
        session.commit()
        token = oauth_tokens.get_decrypted(
            session, provider="google", account_key=token.account_key
        )
        assert token is not None
    return GoogleCalendarClient(token.access_token)


def _rfc3339(dt: datetime) -> str:
    return dt.isoformat()


def _tz_name(dt: datetime) -> str:
    # Calendar accepts either an IANA tz name (`Europe/Berlin`) or a UTC
    # offset string; both work as long as it matches the `dateTime` offset.
    name = getattr(dt.tzinfo, "key", None)
    if isinstance(name, str):
        return name
    return str(dt.tzinfo)


def _normalize(item: dict, calendar_id: str) -> CalendarEvent:
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
