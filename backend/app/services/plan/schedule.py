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


async def schedule(session: Session, task) -> None:
    """Pick a slot for `task` and create its calendar mirror.

    No-op when `task.due_date` is None. Calendar failures are swallowed by
    `calendar.add_task_event`; planning failures notify the user and leave the
    task unscheduled.
    """
    if task.due_date is None:
        log.debug("plan.schedule · task=%s has no due_date, skipping", getattr(task, "id", "?"))
        return

    try:
        start, end = await plan_task_slot(session, task)
    except ValueError:
        await notify_no_slot(task)
        return

    from app.services.calendar import add_task_event

    await add_task_event(session, task, start=start, end=end)
    if task.calendar_event_id:
        from app.services.plan.commute import plan_commutes_window_best_effort

        margin = _commute_window_margin()
        await plan_commutes_window_best_effort(
            session,
            window_start=start - margin,
            window_end=end + margin,
            target_event_ids={task.calendar_event_id},
            stale_event_ids={task.calendar_event_id},
        )


async def plan_task_slot(
    session: Session,
    task,
    *,
    extra_busy: list[Interval] | None = None,
    account_key: str | None = None,
) -> tuple[datetime, datetime]:
    """Return the earliest valid slot for `task`.

    Raises `ValueError` when the task has no due date or no slot can be found
    in the search window.
    """
    if task.due_date is None:
        raise ValueError("task has no due_date")

    settings = get_settings()
    due_local = _to_local(task.due_date)
    now_local = datetime.now(timezone.utc).astimezone(_user_tz())
    search_start = due_local - timedelta(days=LEAD_DAYS)
    if search_start < now_local:
        search_start = now_local
    search_end = due_local if due_local > search_start else search_start + timedelta(days=LEAD_DAYS)

    busy = await _fetch_busy(
        session,
        _busy_calendar_ids(settings),
        search_start,
        search_end,
        getattr(task, "calendar_event_id", None),
        account_key=account_key,
    )
    busy.extend(extra_busy or [])

    slot = _pick_slot(
        busy,
        _duration_minutes(task, settings),
        search_start,
        search_end,
    )
    if slot is None:
        raise ValueError("no free slot before due date")
    return slot


async def plan_tasks(
    session: Session,
    tasks,
    *,
    account_key: str | None = None,
) -> dict[uuid.UUID, tuple[datetime, datetime]]:
    """Plan many tasks urgent-first and reserve earlier picks in memory."""
    ordered = sorted(
        [task for task in tasks if task.due_date is not None],
        key=lambda task: task.due_date,
    )
    planned: dict[uuid.UUID, tuple[datetime, datetime]] = {}
    reserved: list[Interval] = []
    for task in ordered:
        try:
            start, end = await plan_task_slot(
                session,
                task,
                extra_busy=reserved,
                account_key=account_key,
            )
        except ValueError:
            await notify_no_slot(task)
            continue
        planned[task.id] = (start, end)
        reserved.append(Interval(start, end))
    return planned


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


async def notify_no_slot(task) -> None:
    if task.due_date is None:
        return
    due_local = _to_local(task.due_date)
    # Lazy import: `notifications` is a sibling module and pulling it at
    # top-level would tighten the import graph for a rarely-fired path.
    from app.services.notify import notify

    title = getattr(task, "title", None) or "Task"
    log.warning(
        "plan.schedule · no free slot for task=%s before due=%s",
        getattr(task, "id", "?"), due_local.isoformat(),
    )
    await notify(
        title="No free slot for task",
        message=f"{title[:120]} — due {due_local.strftime('%b %d, %H:%M')}. Schedule manually.",
    )


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
    *,
    account_key: str | None = None,
) -> list[Interval]:
    from app.services.calendar.client import normalize
    from app.services.calendar.client import authorized_client

    if not calendar_ids:
        return []

    client = await authorized_client(session, account_key)
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
            if ev.all_day:
                continue
            tz = _user_tz()
            out.append(Interval(ev.start.astimezone(tz), ev.end.astimezone(tz)))
    return out


def _duration_minutes(task, settings) -> int:
    raw = task.estimation or settings.google_calendar_default_event_minutes
    return max(5, int(raw or 30))


def _commute_window_margin() -> timedelta:
    settings = get_settings()
    return timedelta(
        minutes=max(
            settings.commute_bike_max_minutes,
            settings.commute_home_layover_minutes * 2,
            settings.commute_event_buffer_minutes,
        )
    )
