"""Live Google Calendar search for the events filter.

The `documents` cache answers instantly, but it trails Google by a sync cycle
and holds only a year of each recurring series. This asks Google itself
(`events.list?q=`) for upcoming matches; the cache keeps every past event, so
only the future can be missing from it. Best effort like Drive: no grant, a
timeout or an API error is an empty list.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.engine import release_read_connection
from app.db.schemas.search import SearchHit
from app.services.calendar.client import CalendarEvent, authorized_client, normalize
from app.services.calendar.events import is_managed_event
from app.services.search.retrieve import calendar_event_hit

log = logging.getLogger(__name__)

# The instances of one recurring series crowd a page, so fetch well past `k`
# to still have `k` series once they collapse.
_PAGE = 50


async def search_calendar_live(
    session: Session, query: str, *, k: int, timeout: float,
) -> list[SearchHit]:
    query = query.strip()
    settings = get_settings()
    # The calendars the search cache mirrors. Search leaves out the write
    # calendar (task mirrors) — see `exclude_calendar_id` in suggest.
    calendar_ids = [
        cid for cid in settings.google_busy_calendar_ids if cid != settings.google_calendar_id
    ]
    if not query or not calendar_ids:
        return []
    try:
        return await asyncio.wait_for(_search(session, query, calendar_ids, k), timeout=timeout)
    except (asyncio.TimeoutError, RuntimeError, httpx.HTTPError) as exc:
        log.info("calendar search skipped · %s: %s", type(exc).__name__, exc)
        return []


async def _search(session: Session, query: str, calendar_ids: list[str], k: int) -> list[SearchHit]:
    client = await authorized_client(session, None)
    release_read_connection(session)
    now = datetime.now(timezone.utc)
    pages = await asyncio.gather(*(
        client.search_events(cid, query, time_min=now, limit=_PAGE) for cid in calendar_ids
    ))
    events = [
        event
        for cid, items in zip(calendar_ids, pages)
        for event in (normalize(item, cid) for item in items)
        if not is_managed_event(event)
    ]
    return [calendar_event_hit(event) for event in next_per_series(events)[:k]]


def next_per_series(events: list[CalendarEvent]) -> list[CalendarEvent]:
    """Soonest first, one per recurring series: its next instance. A series is
    keyed by title like the cache's suggest (`_SERIES_KEY`) — an organizer
    re-creating a meeting starts a new series of the same meeting."""
    first: dict[str, CalendarEvent] = {}
    for event in sorted(events, key=lambda e: e.start):
        key = f"series:{event.summary.lower()}" if event.raw.get("recurringEventId") else event.id
        first.setdefault(key, event)
    return list(first.values())
