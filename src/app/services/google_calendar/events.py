"""Top-level calendar operations: list / create / patch events.

Auth piggybacks on the same Google OAuth bundle the Gmail source uses; the
required scope (`calendar.events`) lives in `app.auth.google`. The caller
hands in a Session and (optionally) an `account_key`; this module handles
token refresh and returns normalized `CalendarEvent` rows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.auth import get_provider
from app.services.google_calendar.client import (
    CalendarEvent,
    GoogleCalendarClient,
    normalize,
    rfc3339,
    tz_name,
)
from app.storage import oauth_tokens

log = logging.getLogger(__name__)

WINDOW_DAYS = 7


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

    client = await authorized_client(session, account_key)
    events: list[CalendarEvent] = []
    for cid in ids:
        log.info(
            "calendar list · id=%s window=%s..%s",
            cid, time_min.isoformat(), time_max.isoformat(),
        )
        items = await client.list_events(cid, time_min=time_min, time_max=time_max)
        events.extend(normalize(it, cid) for it in items)
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
    color_id: str | None = None,
    account_key: str | None = None,
) -> CalendarEvent:
    """Create an event on `calendar_id`. `start`/`end` must be tz-aware."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")

    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": rfc3339(start), "timeZone": tz_name(start)},
        "end": {"dateTime": rfc3339(end), "timeZone": tz_name(end)},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if color_id:
        body["colorId"] = color_id

    client = await authorized_client(session, account_key)
    log.info("calendar insert · calendar=%s summary=%r", calendar_id, summary)
    raw = await client.insert_event(calendar_id, body)
    return normalize(raw, calendar_id)


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
    color_id: str | None = None,
    account_key: str | None = None,
) -> CalendarEvent:
    """Patch an existing event on `calendar_id`. Only provided fields change."""
    body: dict[str, Any] = {}
    if summary is not None:
        body["summary"] = summary
    if start is not None:
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        body["start"] = {"dateTime": rfc3339(start), "timeZone": tz_name(start)}
    if end is not None:
        if end.tzinfo is None:
            raise ValueError("end must be timezone-aware")
        body["end"] = {"dateTime": rfc3339(end), "timeZone": tz_name(end)}
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if color_id is not None:
        body["colorId"] = color_id

    client = await authorized_client(session, account_key)
    log.info("calendar patch · calendar=%s event=%s", calendar_id, event_id)
    raw = await client.patch_event(calendar_id, event_id, body)
    return normalize(raw, calendar_id)


async def delete_event(
    session: Session,
    *,
    calendar_id: str,
    event_id: str,
    account_key: str | None = None,
) -> None:
    """Delete an event on `calendar_id`."""
    client = await authorized_client(session, account_key)
    log.info("calendar delete · calendar=%s event=%s", calendar_id, event_id)
    await client.delete_event(calendar_id, event_id)


async def authorized_client(
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
