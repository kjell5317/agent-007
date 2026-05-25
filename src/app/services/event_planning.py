"""Pick a `(start, end)` slot for a task on the primary calendar.

Rules:
- Aim for one week before `due_date`. If that's already in the past,
  search forward from now.
- Slots must lie inside the preferred daily window `10:00–20:00` (local).
- If nothing fits before `due_date`, fall back to the extended window
  `08:00–24:00`.
- Slots must not overlap existing events on the primary calendar.
- Batch planning (`plan_tasks`) processes tasks shortest-due-date first
  so the most urgent claim the best slot.

Local timezone is whatever `Settings.user_timezone` resolves to (default UTC).
Container-default UTC was producing scheduling at "8:00 UTC = 10:00 CEST" and
similar surprises; setting `USER_TIMEZONE=Europe/Berlin` fixes it without
relying on the container's `TZ` env var.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.config import get_settings

log = logging.getLogger(__name__)


def _user_tz() -> ZoneInfo | timezone:
    """User's configured IANA timezone, falling back to UTC if unset/invalid.

    Centralized so every conversion in this module picks the same zone — the
    scheduling windows (`PREFERRED_WINDOW`, `EXTENDED_WINDOW`) are wall-clock
    hours and only make sense relative to a definite zone, not whatever the
    container's local zone happens to be.
    """
    name = (get_settings().user_timezone or "UTC").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("user_timezone=%r not found; falling back to UTC", name)
        return timezone.utc

PREFERRED_WINDOW: tuple[time, time] = (time(10, 0), time(20, 0))
# `time(0, 0)` here means "midnight at end of day"; resolved by `_day_window`.
EXTENDED_WINDOW: tuple[time, time] = (time(8, 0), time(0, 0))

LEAD_DAYS = 7


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


async def plan_task_slot(
    session: Session,
    task,
    *,
    now: datetime | None = None,
    extra_busy: list[Interval] | None = None,
) -> tuple[datetime, datetime]:
    """Return the chosen `(start, end)` for `task`, both tz-aware (local).

    Busy calendars (the target `google_calendar_id` plus any
    `google_busy_calendar_ids`) are scanned for conflicts. `extra_busy`
    lets a batch caller pass already-chosen slots from earlier tasks.
    The task's own existing event, if any, is excluded from busy so a
    re-plan doesn't block itself.

    When no slot exists between `now` and `due_date` (even under the
    extended 08:00–24:00 window), a Home Assistant notification is
    fired and we flush the event against `due_date` so calendar sync
    still produces a placeholder the user can drag manually.
    """
    settings = get_settings()
    duration = task.estimation or settings.google_calendar_default_event_minutes
    tz = _user_tz()
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    due_local = _to_local(task.due_date)

    if due_local <= now_local + timedelta(minutes=duration):
        return now_local, now_local + timedelta(minutes=duration)

    target = due_local - timedelta(days=LEAD_DAYS)
    search_start = max(now_local, target)

    busy = await _fetch_busy(
        session,
        _busy_calendar_ids(settings),
        search_start,
        due_local,
        getattr(task, "calendar_event_id", None),
    )
    if extra_busy:
        busy.extend(extra_busy)

    slot = _pick_slot(busy, duration, search_start, due_local)
    if slot is not None:
        return slot

    await _notify_no_slot(task, due_local)
    return due_local - timedelta(minutes=duration), due_local


def _busy_calendar_ids(settings) -> list[str]:
    target = (settings.google_calendar_id or "").strip()
    extras = [c.strip() for c in settings.google_busy_calendar_ids if c.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for cid in [target, *extras]:
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


async def _notify_no_slot(task, due_local: datetime) -> None:
    # Lazy import: `notifications` is a sibling module and pulling it at
    # top-level would tighten the import graph for a rarely-fired path.
    from app.services.notifications import notify

    title = getattr(task, "title", None) or "Task"
    log.warning(
        "event_planning · no free slot for task=%s before due=%s",
        getattr(task, "id", "?"), due_local.isoformat(),
    )
    await notify(
        title="No free slot for task",
        message=f"{title[:120]} — due {due_local.strftime('%b %d, %H:%M')}. Schedule manually.",
    )


async def plan_tasks(
    session: Session,
    tasks: list,
    *,
    now: datetime | None = None,
) -> dict[uuid.UUID, tuple[datetime, datetime]]:
    """Plan multiple tasks shortest-due-date first.

    Each task is scheduled against the live calendars plus the slots
    already picked for earlier (more urgent) tasks in this batch.
    Tasks without a due_date are skipped.
    """
    queued = [t for t in tasks if t.due_date is not None]
    queued.sort(key=lambda t: _to_local(t.due_date))

    chosen: dict[uuid.UUID, tuple[datetime, datetime]] = {}
    extras: list[Interval] = []
    for t in queued:
        start, end = await plan_task_slot(session, t, now=now, extra_busy=extras)
        chosen[t.id] = (start, end)
        extras.append(Interval(start, end))
    return chosen


def _pick_slot(
    busy: list[Interval],
    duration_minutes: int,
    search_start: datetime,
    search_end: datetime,
) -> tuple[datetime, datetime] | None:
    for day_start, day_end in (PREFERRED_WINDOW, EXTENDED_WINDOW):
        slot = _find_slot(busy, duration_minutes, search_start, search_end, day_start, day_end)
        if slot is not None:
            return slot
    return None


def _find_slot(
    busy: list[Interval],
    duration_minutes: int,
    search_start: datetime,
    search_end: datetime,
    day_start: time,
    day_end: time,
) -> tuple[datetime, datetime] | None:
    """Earliest free `duration_minutes` slot fully inside the daily window."""
    duration = timedelta(minutes=duration_minutes)
    ordered = sorted(busy, key=lambda i: i.start)

    cursor = search_start
    while cursor + duration <= search_end:
        win_start, win_end = _day_window(cursor, day_start, day_end)
        if cursor < win_start:
            cursor = win_start
            continue
        if cursor >= win_end:
            cursor = _next_day_start(cursor)
            continue

        slot_end = cursor + duration
        if slot_end > win_end:
            cursor = _next_day_start(cursor)
            continue
        if slot_end > search_end:
            return None

        conflict = next(
            (b for b in ordered if b.start < slot_end and cursor < b.end),
            None,
        )
        if conflict is None:
            return cursor, slot_end
        cursor = conflict.end

    return None


def _day_window(anchor: datetime, day_start: time, day_end: time) -> tuple[datetime, datetime]:
    win_start = anchor.replace(
        hour=day_start.hour, minute=day_start.minute, second=0, microsecond=0,
    )
    if day_end == time(0, 0):
        win_end = _next_day_start(anchor)
    else:
        win_end = anchor.replace(
            hour=day_end.hour, minute=day_end.minute, second=0, microsecond=0,
        )
    return win_start, win_end


def _next_day_start(anchor: datetime) -> datetime:
    return (anchor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_user_tz())


async def _fetch_busy(
    session: Session,
    calendar_ids: list[str],
    time_min: datetime,
    time_max: datetime,
    exclude_event_id: str | None,
) -> list[Interval]:
    # Imported lazily to break the cycle with `google_calendar.sync`, which
    # imports this module.
    from app.services.google_calendar.client import normalize
    from app.services.google_calendar.events import authorized_client

    if not calendar_ids:
        return []

    client = await authorized_client(session, None)
    out: list[Interval] = []
    for cid in calendar_ids:
        items = await client.list_events(cid, time_min=time_min, time_max=time_max)
        for raw in items:
            if exclude_event_id and raw.get("id") == exclude_event_id:
                continue
            if raw.get("status") == "cancelled":
                continue
            # Events marked free (transparency=transparent) don't block other bookings.
            if raw.get("transparency") == "transparent":
                continue
            ev = normalize(raw, cid)
            tz = _user_tz()
            out.append(Interval(ev.start.astimezone(tz), ev.end.astimezone(tz)))
    return out
