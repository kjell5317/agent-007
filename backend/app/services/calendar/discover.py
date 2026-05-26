"""Discover externally-modified calendar events.

Polls Google Calendar with `updatedMin` set to the last check time and
asks: "what changed?". Any event that returns AND now overlaps with
another event in the look-ahead window gets handed off to
`services.plan.reschedule.reschedule_event` — the plan layer owns
deciding what to do about the conflict.

The cursor is stored per-account on the oauth_tokens row (extra JSON),
mirroring the way `gmail/poll.py` persists its `history_id` watermark.
On the first run for an account the cursor is bootstrapped to "now
minus one day" so we don't try to ingest the whole calendar history.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.clients import oauth_tokens
from app.services.calendar.client import CalendarEvent, authorized_client, normalize
from app.services.calendar.events import WINDOW_DAYS
from app.services.plan.reschedule import reschedule

log = logging.getLogger(__name__)

# How far back to look on the very first run (no cursor yet).
INITIAL_LOOKBACK_DAYS = 1

# Cursor key inside oauth_tokens.extra.
_CURSOR_KEY = "calendar_updated_min"


async def discover_updated_events(
    session: Session,
    *,
    calendar_ids: Iterable[str],
    account_key: str | None = None,
) -> dict:
    """Fetch events changed since the last poll; reschedule any that now overlap.

    Returns a summary dict:
      * `checked`      — total events seen in the look-ahead window
      * `updated`      — events changed since the last cursor
      * `overlapping`  — updated events that triggered a reschedule call
      * `cursor`       — ISO timestamp of the new cursor we persisted
    """
    ids = list(calendar_ids)
    if not ids:
        return _empty_summary()

    token = oauth_tokens.get_decrypted(session, provider="google", account_key=account_key)
    if token is None:
        return _empty_summary()

    now = datetime.now(timezone.utc)
    cursor_iso = (token.extra or {}).get(_CURSOR_KEY)
    if cursor_iso:
        updated_min = datetime.fromisoformat(cursor_iso)
    else:
        updated_min = now - timedelta(days=INITIAL_LOOKBACK_DAYS)

    # Look one day back to catch in-progress events whose start drifted earlier,
    # and WINDOW_DAYS forward so an updated event has neighbours to compare to.
    window_start = now - timedelta(days=1)
    window_end = now + timedelta(days=WINDOW_DAYS)

    client = await authorized_client(session, account_key)

    summary: dict = {"checked": 0, "updated": 0, "overlapping": 0, "cursor": now.isoformat()}

    for cid in ids:
        log.info(
            "calendar discover · id=%s updated_min=%s window=%s..%s",
            cid, updated_min.isoformat(), window_start.isoformat(), window_end.isoformat(),
        )

        # Cheap call first: ask only for events touched since the cursor.
        # In the common case (nothing changed) we skip the wider listing and
        # save a round trip per calendar.
        updated_items = await client.list_events(
            cid,
            time_min=window_start,
            time_max=window_end,
            updated_min=updated_min,
        )
        if not updated_items:
            continue
        updated_events = [normalize(it, cid) for it in updated_items]
        summary["updated"] += len(updated_events)

        # Something changed → now pull the full window so we have neighbours
        # to check overlaps against.
        items = await client.list_events(cid, time_min=window_start, time_max=window_end)
        all_events = [normalize(it, cid) for it in items]
        summary["checked"] += len(all_events)

        for ev in updated_events:
            if _overlaps_any(ev, all_events):
                summary["overlapping"] += 1
                log.info(
                    "discover · event=%s overlaps another in window; triggering reschedule",
                    ev.id,
                )
                await reschedule(
                    session,
                    event_id=ev.id,
                    account_key=account_key,
                )

    # Advance the cursor only after a successful pass. If anything above raised
    # we'll re-check the same window on the next run, which is the desired
    # behaviour — overlap handling stays at-least-once.
    oauth_tokens.set_extra(
        session,
        provider="google",
        account_key=token.account_key,
        patch={_CURSOR_KEY: now.isoformat()},
    )
    session.commit()

    return summary


def _overlaps_any(event: CalendarEvent, others: Iterable[CalendarEvent]) -> bool:
    """True if `event` overlaps in time with any *other* event in `others`.

    Skips all-day events and skips comparing the event against itself. Two
    intervals [a, b) overlap iff `a.start < b.end and b.start < a.end`.
    """
    if event.all_day:
        return False
    for other in others:
        if other.id == event.id or other.all_day:
            continue
        if event.start < other.end and other.start < event.end:
            return True
    return False


def _empty_summary() -> dict:
    return {"checked": 0, "updated": 0, "overlapping": 0, "cursor": None}
