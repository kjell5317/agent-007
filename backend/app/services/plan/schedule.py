"""Task slot planning.

The planner has two phases:

1. Find a clean free slot without moving anything.
2. Only if no free slot exists before the deadline, move one less-urgent
   managed task out of the way and place the current task in that freed slot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.task import Task

log = logging.getLogger(__name__)

LEAD_DAYS = 7
DAY_START = time(10, 0)
DAY_TARGET = time(20, 0)
# Used when the user taps the "extend" action on a no-slot notification:
# widen each day's search window so the next attempt has more room.
EXTENDED_DAY_START = time(8, 0)
EXTENDED_DAY_TARGET = time(23, 59, 59)
MAX_REPAIR_DEPTH = 8


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    event_id: str | None = None


@dataclass(frozen=True)
class BusyEvent:
    id: str
    start: datetime
    end: datetime
    kind: str


async def schedule(
    session: Session,
    task: Task | None = None,
    *,
    event_id: str | None = None,
    block: Interval | None = None,
    account_key: str | None = None,
    extend_window: bool = False,
    _depth: int = 0,
) -> None:
    """Plan `task` and create/update its calendar mirror.

    `event_id` is accepted for discover/repair callers that only know the
    managed calendar event that collided.

    `extend_window=True` widens each day's search range to 08:00–24:00
    (default 10:00–20:00). Set by the HA "extend" action button on a
    previous no-slot notification.
    """
    if task is None and event_id is not None:
        task = _task_for_event(session, event_id)
    if task is None:
        if event_id:
            await _repair_commute_event(session, event_id, account_key=account_key)
        return
    if task.due_date is None:
        log.debug("plan.schedule · task=%s has no due_date", task.id)
        return

    is_fresh = task.calendar_event_id is None

    try:
        start, end = await plan_task_slot(
            session,
            task,
            block=block,
            account_key=account_key,
            extend_window=extend_window,
            _depth=_depth,
        )
    except ValueError:
        from app.services.notify import notify_no_slot

        log.warning(
            "plan.schedule · no slot for task=%s due=%s extended=%s",
            task.id, task.due_date.isoformat(), extend_window,
        )
        await notify_no_slot(task, extended=extend_window)
        return

    from app.services.calendar import add_task_event, update_task_event
    from app.services.notify import notify_task_scheduled

    if task.calendar_event_id:
        await update_task_event(session, task, start=start, end=end)
    else:
        await add_task_event(session, task, start=start, end=end)

    await _replan_commutes_around(
        session,
        task,
        start=start,
        end=end,
        account_key=account_key,
    )

    await notify_task_scheduled(task, start=start, end=end, is_fresh=is_fresh)


async def plan_task_slot(
    session: Session,
    task: Task,
    *,
    extra_busy: list[Interval] | None = None,
    block: Interval | None = None,
    account_key: str | None = None,
    extend_window: bool = False,
    _depth: int = 0,
) -> tuple[datetime, datetime]:
    """Return a planned `(start, end)` for `task`.

    The free search scans days forward from `max(now, due - 7d)`. Within
    each day it starts at the target time and moves backward toward the
    start time. `extend_window=True` widens the per-day range from
    10:00–20:00 to 08:00–24:00.
    """
    if task.due_date is None:
        raise ValueError("task has no due_date")

    settings = get_settings()
    due = _to_local(task.due_date)
    now = datetime.now(timezone.utc).astimezone(_user_tz())
    window_start = max(now, due - timedelta(days=LEAD_DAYS))
    window_end = due
    if window_end <= window_start:
        raise ValueError("deadline is in the past")

    busy = await _fetch_busy_events(
        session,
        window_start,
        window_end,
        exclude_event_id=task.calendar_event_id,
        account_key=account_key,
    )
    for itv in extra_busy or []:
        busy.append(BusyEvent(itv.event_id or "extra", itv.start, itv.end, "extra"))
    if block is not None:
        busy.append(BusyEvent(block.event_id or "block", block.start, block.end, "block"))

    duration = timedelta(minutes=_duration_minutes(task, settings))
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    slot = _find_free_slot(
        busy, duration, window_start, window_end, buffer, extend_window=extend_window
    )
    if slot is not None:
        return slot

    return await _repair_by_displacing_task(
        session,
        task,
        busy,
        duration=duration,
        window_start=window_start,
        window_end=window_end,
        account_key=account_key,
        depth=_depth,
    )


def _find_free_slot(
    busy: list[BusyEvent],
    duration: timedelta,
    window_start: datetime,
    window_end: datetime,
    buffer: timedelta,
    *,
    extend_window: bool = False,
) -> tuple[datetime, datetime] | None:
    ordered = sorted(busy, key=lambda ev: ev.start)
    day = window_start.date()
    last_day = window_end.date()
    tz = _user_tz()
    day_start = EXTENDED_DAY_START if extend_window else DAY_START
    day_target = EXTENDED_DAY_TARGET if extend_window else DAY_TARGET

    while day <= last_day:
        lower = datetime.combine(day, day_start, tzinfo=tz)
        upper = datetime.combine(day, day_target, tzinfo=tz)
        lower = max(lower, window_start)
        upper = min(upper, window_end)
        cursor = upper

        while cursor - duration >= lower:
            start = cursor - duration
            end = cursor
            conflict = _latest_conflict(ordered, start - buffer, end + buffer)
            if conflict is None:
                return start, end
            cursor = min(cursor, conflict.start - buffer)

        day = day + timedelta(days=1)

    return None


async def _repair_by_displacing_task(
    session: Session,
    task: Task,
    busy: list[BusyEvent],
    *,
    duration: timedelta,
    window_start: datetime,
    window_end: datetime,
    account_key: str | None,
    depth: int,
) -> tuple[datetime, datetime]:
    if depth >= MAX_REPAIR_DEPTH:
        raise ValueError("repair recursion limit reached")

    victims = _movable_victims(session, task, busy, duration)
    for victim, victim_event in victims:
        freed = Interval(victim_event.start, victim_event.end, victim_event.id)
        try:
            await schedule(
                session,
                victim,
                block=freed,
                account_key=account_key,
                _depth=depth + 1,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan.schedule · victim move failed task=%s err=%s", victim.id, exc)
            continue

        if freed.start < window_start or freed.end > window_end:
            continue
        if freed.end - duration < freed.start:
            continue
        return freed.end - duration, freed.end

    raise ValueError("no free slot before due date")


def _movable_victims(
    session: Session,
    task: Task,
    busy: list[BusyEvent],
    duration: timedelta,
) -> list[tuple[Task, BusyEvent]]:
    event_by_id = {
        ev.id: ev for ev in busy if ev.kind == "task" and ev.end - ev.start >= duration
    }
    if not event_by_id:
        return []
    stmt = (
        select(Task)
        .where(Task.calendar_event_id.in_(list(event_by_id)))
        .where(Task.id != task.id)
        .where(Task.due_date.is_not(None))
    )
    rows = list(session.execute(stmt).scalars())
    rows.sort(key=lambda row: row.due_date or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
    return [(row, event_by_id[row.calendar_event_id]) for row in rows if row.calendar_event_id]


def _latest_conflict(events: list[BusyEvent], start: datetime, end: datetime) -> BusyEvent | None:
    conflicts = [ev for ev in events if ev.start < end and start < ev.end]
    if not conflicts:
        return None
    return max(conflicts, key=lambda ev: ev.start)


async def _fetch_busy_events(
    session: Session,
    time_min: datetime,
    time_max: datetime,
    *,
    exclude_event_id: str | None,
    account_key: str | None,
) -> list[BusyEvent]:
    from app.services.calendar import is_commute_event, is_task_event, list_events_between

    settings = get_settings()
    ids = _busy_calendar_ids(settings)
    if not ids:
        return []
    events = await list_events_between(
        session,
        calendar_ids=ids,
        time_min=time_min,
        time_max=time_max,
        account_key=account_key,
    )
    out: list[BusyEvent] = []
    for ev in events:
        if ev.id == exclude_event_id or ev.all_day:
            continue
        kind = "commute" if is_commute_event(ev) else "task" if is_task_event(ev) else "busy"
        out.append(BusyEvent(ev.id, _to_local(ev.start), _to_local(ev.end), kind))
    return out


async def _repair_commute_event(
    session: Session,
    event_id: str,
    *,
    account_key: str | None,
) -> None:
    from app.services.calendar import get_event, is_commute_event, private_properties
    from app.services.plan.commute import plan_commutes_window_best_effort

    calendar_id = (get_settings().google_calendar_id or "").strip()
    if not calendar_id:
        return
    try:
        event = await get_event(session, calendar_id=calendar_id, event_id=event_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.schedule · get event failed event=%s err=%s", event_id, exc)
        return
    if not is_commute_event(event):
        return
    related = private_properties(event).get("related_event_id")
    await plan_commutes_window_best_effort(
        session,
        window_start=event.start,
        window_end=event.end,
        target_event_ids={related} if related else None,
        stale_event_ids={related} if related else None,
        account_key=account_key,
    )


async def _replan_commutes_around(
    session: Session,
    task: Task,
    *,
    start: datetime,
    end: datetime,
    account_key: str | None,
) -> None:
    if not task.calendar_event_id:
        return
    from app.services.plan.commute import plan_commutes_window_best_effort

    margin = _commute_window_margin()
    event_ids = {task.calendar_event_id}
    await plan_commutes_window_best_effort(
        session,
        window_start=start - margin,
        window_end=end + margin,
        target_event_ids=event_ids if (task.location or "").strip() else None,
        stale_event_ids=event_ids,
        account_key=account_key,
    )


def _task_for_event(session: Session, event_id: str) -> Task | None:
    stmt = select(Task).where(Task.calendar_event_id == event_id)
    return session.execute(stmt).scalar_one_or_none()


def _duration_minutes(task: Task, settings) -> int:
    raw = task.estimation or settings.google_calendar_default_event_minutes
    return max(5, int(raw or 30))


def _busy_calendar_ids(settings) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for cid in [settings.google_calendar_id, *settings.google_busy_calendar_ids]:
        clean = (cid or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _commute_window_margin() -> timedelta:
    settings = get_settings()
    return timedelta(
        minutes=max(
            settings.commute_bike_max_minutes,
            settings.commute_home_layover_minutes * 2,
            settings.commute_event_buffer_minutes,
        )
    )


def _to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_user_tz())


def _user_tz() -> ZoneInfo | timezone:
    name = (get_settings().user_timezone or "UTC").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("user_timezone=%r not found; falling back to UTC", name)
        return timezone.utc
