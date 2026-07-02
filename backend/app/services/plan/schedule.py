"""Task slot planning.

The planner has two phases:

1. Find a clean free slot without moving anything.
2. Only if no free slot exists before the deadline, move one less-urgent
   managed task out of the way and place the current task in that freed slot.

Two entry points:

  * `schedule_task(task, ...)` — caller has a Task. All in-app paths use this.
  * `reschedule_event(event_id, ...)` — caller only knows the calendar event
    id (calendar-discover). Dispatches to schedule_task when the event maps
    to a Task, or repairs the commute plan when it doesn't.

`schedule_task` is serialized process-wide via a single asyncio.Lock: the
busy view is a live calendar read, so the whole plan→write section has to be
atomic across *all* tasks, not just per task. Concurrent triggers (cron polls,
HA action, queue worker — all one event loop) would otherwise each read a
snapshot missing the other's not-yet-written event and place overlapping slots.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.task import Task
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

LEAD_DAYS = 7
DAY_START = time(10, 0)
DAY_TARGET = time(20, 0)
# Extended-mode bounds. The normal `[DAY_START, DAY_TARGET]` range is
# skipped in extended mode (it was already exhausted on the first attempt
# that triggered the "no slot" notification). Instead, each day is
# scanned in two phases:
#   1. late evening — forward from DAY_TARGET to END_OF_DAY (20→24)
#   2. early morning — backward from DAY_START to EARLY_MORNING (10→8)
# then the next day, same procedure.
EARLY_MORNING = time(8, 0)
END_OF_DAY = time(23, 59, 59)
MAX_REPAIR_DEPTH = 8

# Process-wide scheduling lock. Held across the entire plan→write section so no
# two placements ever race on the same stale calendar snapshot. See module docs.
_schedule_lock = asyncio.Lock()


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


async def schedule_task(
    session: Session,
    task: Task,
    *,
    block: Interval | None = None,
    account_key: str | None = None,
    notify: bool = True,
    _depth: int = 0,
) -> tuple[datetime, datetime] | None:
    """Plan `task` and create/update its calendar mirror.

    The planner searches the normal 10:00–20:00 window first. If that
    cannot place the task, it automatically tries the extended 08:00–24:00
    fallback before reporting no slot.

    `notify=False` suppresses the per-task scheduled/no-slot notification —
    batch callers (commute reschedule of many tasks) handle their own
    aggregated notification.

    Returns the placed `(start, end)` on success, `None` otherwise.
    """
    if task.due_date is None:
        log.debug("plan.schedule · task=%s has no due_date", task.id)
        return None

    # Victim reschedules recurse with _depth>0 and already run inside the lock;
    # re-acquiring would deadlock (asyncio.Lock isn't reentrant).
    if _depth > 0:
        return await _schedule_task_locked(
            session,
            task,
            block=block,
            account_key=account_key,
            notify=notify,
            _depth=_depth,
        )

    async with _schedule_lock:
        return await _schedule_task_locked(
            session,
            task,
            block=block,
            account_key=account_key,
            notify=notify,
            _depth=_depth,
        )


async def _schedule_task_locked(
    session: Session,
    task: Task,
    *,
    block: Interval | None,
    account_key: str | None,
    notify: bool,
    _depth: int,
) -> tuple[datetime, datetime] | None:
    is_fresh = task.calendar_event_id is None

    try:
        start, end = await plan_task_slot(
            session,
            task,
            block=block,
            account_key=account_key,
            _depth=_depth,
        )
    except ValueError:
        log.warning(
            "plan.schedule · no slot for task=%s due=%s",
            task.id, task.due_date.isoformat() if task.due_date else None,
        )
        if notify:
            from app.services.notify import notify_no_slot

            await notify_no_slot(task)
        return None

    from app.services.calendar import add_task_event, update_task_event

    try:
        if task.calendar_event_id:
            await update_task_event(session, task, start=start, end=end)
        else:
            await add_task_event(session, task, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        # A "Scheduled" notification past this point would lie — the slot
        # exists in the planner's head but not on the calendar. Log loudly
        # and surface to the phone so the user notices instead of finding
        # out the next time they open Google Calendar.
        log.error(
            "plan.schedule · calendar write failed task=%s err=%s",
            task.id, exc, exc_info=True,
        )
        if notify:
            from app.services.notify import notify_error

            await notify_error(
                f"Calendar write failed: {task.title[:80]}",
                exc,
                context=f"task_id={task.id}",
            )
        return None

    await _replan_commutes_around(
        session,
        task,
        start=start,
        end=end,
        account_key=account_key,
    )

    if notify:
        if is_fresh:
            # First slot for this task — announce it. Shares the task tag, so
            # it also replaces any lingering "could not schedule" warning.
            from app.services.notify import notify_task_created

            await notify_task_created(task, start=start, end=end)
        else:
            # A silent reschedule of an already-mirrored task. Still clear a
            # possible warning so a recovered task doesn't keep nagging.
            from app.services.notify import clear_task_notification

            await clear_task_notification(task.id)
    return start, end


async def reschedule_event(
    session: Session,
    event_id: str,
    *,
    account_key: str | None = None,
) -> None:
    """Dispatch a calendar-discover overlap.

    If the event maps to a Task, re-plan that task. Otherwise treat it as a
    managed commute event and recompute the commute plan around it.
    """
    task = _task_for_event(session, event_id)
    if task is not None:
        result = await schedule_task(session, task, account_key=account_key)
        if result is not None:
            from app.events import publish_task

            publish_task(session, task.id)
        return
    await _repair_commute_event(session, event_id, account_key=account_key)


async def plan_task_slot(
    session: Session,
    task: Task,
    *,
    extra_busy: list[Interval] | None = None,
    block: Interval | None = None,
    account_key: str | None = None,
    _depth: int = 0,
) -> tuple[datetime, datetime]:
    """Return a planned `(start, end)` for `task`.

    The free search scans days forward from `max(now, due - 7d)`. Within
    each day it starts at the target time and moves backward toward the
    start time. If the normal 10:00–20:00 window cannot place the task, the
    planner automatically tries the extended 08:00–24:00 fallback before
    raising.
    """
    if task.due_date is None:
        raise ValueError("task has no due_date")

    settings = get_settings()
    due = to_user_tz(task.due_date)
    now = datetime.now(user_tz()) + timedelta(minutes=settings.commute_event_buffer_minutes)
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
    busy.extend(
        _db_scheduled_busy(
            session, task, window_start, window_end, {ev.id for ev in busy}
        )
    )
    for itv in extra_busy or []:
        busy.append(BusyEvent(itv.event_id or "extra", itv.start, itv.end, "extra"))
    if block is not None:
        busy.append(BusyEvent(block.event_id or "block", block.start, block.end, "block"))

    duration = timedelta(minutes=_duration_minutes(task, settings))
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    slot = _find_free_slot(
        busy, duration, window_start, window_end, buffer, extended_window=False
    )
    if slot is not None:
        return slot

    try:
        return await _repair_by_displacing_task(
            session,
            task,
            busy,
            duration=duration,
            window_start=window_start,
            window_end=window_end,
            account_key=account_key,
            depth=_depth,
            extended_window=False,
        )
    except ValueError:
        log.info(
            "plan.schedule · normal window exhausted task=%s due=%s; trying extended window",
            task.id, task.due_date.isoformat() if task.due_date else None,
        )

    slot = _find_free_slot(
        busy, duration, window_start, window_end, buffer, extended_window=True
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
        extended_window=True,
    )


def _find_free_slot(
    busy: list[BusyEvent],
    duration: timedelta,
    window_start: datetime,
    window_end: datetime,
    buffer: timedelta,
    *,
    extended_window: bool = False,
) -> tuple[datetime, datetime] | None:
    ordered = sorted(busy, key=lambda ev: ev.start)
    day = window_start.date()
    last_day = window_end.date()
    tz = user_tz()

    while day <= last_day:
        if extended_window:
            # Phase 1: late evening forward sweep, 20:00 → 24:00.
            lower, upper = _day_bounds(day, DAY_TARGET, END_OF_DAY, tz, window_start, window_end)
            slot = _sweep_forward(ordered, duration, buffer, lower, upper)
            if slot is not None:
                return slot
            # Phase 2: early morning backward sweep, 10:00 → 08:00.
            lower, upper = _day_bounds(day, EARLY_MORNING, DAY_START, tz, window_start, window_end)
            slot = _sweep_backward(ordered, duration, buffer, lower, upper)
            if slot is not None:
                return slot
        else:
            lower, upper = _day_bounds(day, DAY_START, DAY_TARGET, tz, window_start, window_end)
            slot = _sweep_backward(ordered, duration, buffer, lower, upper)
            if slot is not None:
                return slot

        day += timedelta(days=1)

    return None


def _day_bounds(
    day,
    lower_time: time,
    upper_time: time,
    tz,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime]:
    lower = max(datetime.combine(day, lower_time, tzinfo=tz), window_start)
    upper = min(datetime.combine(day, upper_time, tzinfo=tz), window_end)
    return lower, upper


def _sweep_backward(
    busy: list[BusyEvent],
    duration: timedelta,
    buffer: timedelta,
    lower: datetime,
    upper: datetime,
) -> tuple[datetime, datetime] | None:
    """Walk the cursor from `upper` down toward `lower`, looking for a
    `(cursor - duration, cursor)` slot that doesn't collide with `busy`."""
    cursor = upper
    while cursor - duration >= lower:
        start = cursor - duration
        end = cursor
        conflict = _latest_conflict(busy, start - buffer, end + buffer)
        if conflict is None:
            return start, end
        cursor = min(cursor, conflict.start - buffer)
    return None


def _sweep_forward(
    busy: list[BusyEvent],
    duration: timedelta,
    buffer: timedelta,
    lower: datetime,
    upper: datetime,
) -> tuple[datetime, datetime] | None:
    """Walk the cursor from `lower` up toward `upper`, looking for a
    `(cursor, cursor + duration)` slot that doesn't collide with `busy`."""
    cursor = lower
    while cursor + duration <= upper:
        start = cursor
        end = cursor + duration
        conflict = _earliest_conflict(busy, start - buffer, end + buffer)
        if conflict is None:
            return start, end
        cursor = max(cursor, conflict.end + buffer)
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
    extended_window: bool,
) -> tuple[datetime, datetime]:
    if depth >= MAX_REPAIR_DEPTH:
        raise ValueError("repair recursion limit reached")

    settings = get_settings()
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    victims = _movable_victims(
        session, task, busy, duration, buffer, window_start, window_end,
    )
    for victim, victim_event, freed_range in victims:
        block = Interval(victim_event.start, victim_event.end, victim_event.id)
        try:
            victim_slot = await schedule_task(
                session,
                victim,
                block=block,
                account_key=account_key,
                _depth=depth + 1,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan.schedule · victim move failed task=%s err=%s", victim.id, exc)
            continue
        if victim_slot is None:
            # Victim couldn't be rescheduled — its old slot still holds.
            continue

        # Rebuild the busy view: the victim vacated its old position and
        # moved to `victim_slot`. Then sweep the freed range (clamped to
        # the per-day working window) for the new task's slot.
        new_busy = [ev for ev in busy if ev.id != victim_event.id]
        new_busy.append(
            BusyEvent(victim_event.id, victim_slot[0], victim_slot[1], "task")
        )
        slot = _slot_in_range(
            new_busy,
            duration,
            buffer,
            freed_range,
            extended_window=extended_window,
        )
        if slot is not None:
            return slot

    raise ValueError("no free slot before due date")


def _slot_in_range(
    busy: list[BusyEvent],
    duration: timedelta,
    buffer: timedelta,
    freed_range: Interval,
    *,
    extended_window: bool,
) -> tuple[datetime, datetime] | None:
    """Latest-fitting slot inside `freed_range`, clamped to the per-day
    working window."""
    tz = user_tz()
    day = freed_range.start.date()
    last_day = freed_range.end.date()
    while day <= last_day:
        if extended_window:
            lower, upper = _day_bounds(
                day, DAY_TARGET, END_OF_DAY, tz, freed_range.start, freed_range.end,
            )
            slot = _sweep_forward(busy, duration, buffer, lower, upper)
            if slot is not None:
                return slot
            lower, upper = _day_bounds(
                day, EARLY_MORNING, DAY_START, tz, freed_range.start, freed_range.end,
            )
            slot = _sweep_backward(busy, duration, buffer, lower, upper)
            if slot is not None:
                return slot
        else:
            lower, upper = _day_bounds(
                day, DAY_START, DAY_TARGET, tz, freed_range.start, freed_range.end,
            )
            slot = _sweep_backward(busy, duration, buffer, lower, upper)
            if slot is not None:
                return slot
        day += timedelta(days=1)
    return None


def _movable_victims(
    session: Session,
    task: Task,
    busy: list[BusyEvent],
    duration: timedelta,
    buffer: timedelta,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[Task, BusyEvent, Interval]]:
    """Task events whose displacement would free a contiguous range large
    enough for the new task. The freed range counts adjacent gaps — a 30min
    victim sitting in a 1h hole between busy neighbours is still a candidate
    for a 1h task because moving it consolidates the surrounding free time.
    """
    min_span = duration + 2 * buffer
    candidates: list[tuple[BusyEvent, Interval]] = []
    for ev in busy:
        if ev.kind != "task":
            continue
        freed = _effective_freed_range(ev, busy, window_start, window_end)
        if freed.end - freed.start < min_span:
            continue
        candidates.append((ev, freed))
    if not candidates:
        return []

    by_event_id = {ev.id: (ev, freed) for ev, freed in candidates}
    stmt = (
        select(Task)
        .where(Task.calendar_event_id.in_(list(by_event_id)))
        .where(Task.id != task.id)
        .where(Task.due_date.is_not(None))
    )
    rows = list(session.execute(stmt).scalars())
    rows.sort(key=lambda row: row.due_date or datetime.max.replace(tzinfo=timezone.utc), reverse=True)
    return [
        (row, *by_event_id[row.calendar_event_id])
        for row in rows if row.calendar_event_id in by_event_id
    ]


def _effective_freed_range(
    victim: BusyEvent,
    busy: list[BusyEvent],
    window_start: datetime,
    window_end: datetime,
) -> Interval:
    """Contiguous time that opens up if `victim` moves elsewhere.

    Bounded by the nearest non-overlapping busy event before / after the
    victim, falling back to the planning window edges when none exist.
    """
    prev_end = window_start
    next_start = window_end
    for ev in busy:
        if ev.id == victim.id:
            continue
        if ev.end <= victim.start and ev.end > prev_end:
            prev_end = ev.end
        if ev.start >= victim.end and ev.start < next_start:
            next_start = ev.start
    return Interval(prev_end, next_start, victim.id)


def _latest_conflict(events: list[BusyEvent], start: datetime, end: datetime) -> BusyEvent | None:
    conflicts = [ev for ev in events if ev.start < end and start < ev.end]
    if not conflicts:
        return None
    return max(conflicts, key=lambda ev: ev.start)


def _earliest_conflict(events: list[BusyEvent], start: datetime, end: datetime) -> BusyEvent | None:
    conflicts = [ev for ev in events if ev.start < end and start < ev.end]
    if not conflicts:
        return None
    return min(conflicts, key=lambda ev: ev.end)


def _db_scheduled_busy(
    session: Session,
    task: Task,
    window_start: datetime,
    window_end: datetime,
    calendar_event_ids: set[str],
) -> list[BusyEvent]:
    """Backstop for the live calendar read: open tasks whose stored slot the
    calendar may not reflect yet — Google's events.list lags a just-created
    event, or the event was deleted out from under us. Skip any whose event
    already appeared in the calendar busy set. Marked immovable ("busy"): we
    won't try to displace a task we can only see in the DB."""
    from app.db.clients import tasks as tasks_store

    settings = get_settings()
    out: list[BusyEvent] = []
    for row in tasks_store.open_scheduled_between(
        session,
        time_min=window_start,
        time_max=window_end,
        exclude_task_id=task.id,
    ):
        if row.calendar_event_id and row.calendar_event_id in calendar_event_ids:
            continue
        start = to_user_tz(row.scheduled_date)
        end = start + timedelta(minutes=_duration_minutes(row, settings))
        out.append(BusyEvent(row.calendar_event_id or f"db:{row.id}", start, end, "busy"))
    return out


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
        out.append(BusyEvent(ev.id, to_user_tz(ev.start), to_user_tz(ev.end), kind))
    return out


async def _repair_commute_event(
    session: Session,
    event_id: str,
    *,
    account_key: str | None,
) -> None:
    if not get_settings().commute_enabled:
        return
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
    # Task events without a location are never commute targets, and no
    # commutes are keyed to them, so the window scan would be pure waste.
    if not (task.location or "").strip():
        return
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    margin = commute_window_margin()
    event_ids = {task.calendar_event_id}
    await plan_commutes_window_best_effort(
        session,
        window_start=start - margin,
        window_end=end + margin,
        target_event_ids=event_ids,
        stale_event_ids=event_ids,
        account_key=account_key,
    )


def _task_for_event(session: Session, event_id: str) -> Task | None:
    stmt = select(Task).where(Task.calendar_event_id == event_id)
    return session.execute(stmt).scalar_one_or_none()


def scheduled_interval_for(task: Task) -> Interval | None:
    if task.scheduled_date is None:
        return None
    settings = get_settings()
    start = to_user_tz(task.scheduled_date)
    end = start + timedelta(minutes=_duration_minutes(task, settings))
    return Interval(start, end, task.calendar_event_id)


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
