"""Task slot planning.

Located tasks are planned as *trip blocks*: the slot search reserves
`outbound leg + buffer + task + buffer + inbound leg` as one atomic
interval, so a slot only exists where the commutes also fit. Once the task
event is written, the actual legs are derived from the anchor timeline by
the commute planner (`_replan_commutes_around`) — chaining may shrink them
below the reservation, never grow them past it (weather-driven mode flips
are reconciled later by the hourly refresh).

Placement preference for located tasks:

1. *Piggyback* — directly before/after an existing anchor at the same
   location (zero marginal commute).
2. *Chain-insert* — into a gap between two anchors when the detour fits.
3. *Standalone trip* — sweep for the full home↔location block:
   a. a clean free slot without moving anything,
   b. only if none exists before the deadline, move one less-urgent
      managed task out of the way and take the freed range.

Two entry points:

  * `schedule_task(task, ...)` — caller has a Task. All in-app paths use this.
  * `reschedule_event(event_id, ...)` — caller only knows the calendar event
    id (calendar-discover). Dispatches to schedule_task when the event maps
    to a Task, or re-derives commute legs around it when it doesn't.

`schedule_task` is serialized process-wide via a single asyncio.Lock: the
busy view is a live calendar read, so the whole plan→write section has to be
atomic across *all* tasks, not just per task. Concurrent triggers (cron polls,
HA action, queue worker — all one event loop) would otherwise each read a
snapshot missing the other's not-yet-written event and place overlapping
slots. Commute planning runs under the same lock (see
`app.services.plan.commute`); everything already inside it passes
`_depth > 0` so the non-reentrant lock is never re-acquired.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.task import Task
from app.services.commute.legs import FAILED_LEG_SECONDS
from app.services.location import is_online_location, resolve_location_alias
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
# Chain-insert evaluates at most this many gaps (each costs Maps lookups).
MAX_CHAIN_GAPS = 8

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
    location: str | None = None
    leg_key: tuple[str, str] | None = None


@dataclass(frozen=True)
class PlannedSlot:
    """A placed task plus the trip block reserved around it. For tasks
    without legs the block equals the task span. `unroutable` marks a trip
    whose legs are 30-minute failed placeholders because Maps found no
    route."""

    start: datetime
    end: datetime
    block_start: datetime
    block_end: datetime
    out_s: int = 0
    in_s: int = 0
    unroutable: bool = False


async def schedule_task(
    session: Session,
    task: Task,
    *,
    block: Interval | None = None,
    account_key: str | None = None,
    notify: bool = True,
    _depth: int = 0,
) -> tuple[datetime, datetime] | None:
    """Plan `task` and create/update its calendar mirror (plus commute legs).

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

    # Everything at _depth>0 (victim reschedules, commute-triggered moves)
    # already runs inside the lock; re-acquiring would deadlock.
    if _depth > 0:
        planned = await _schedule_task_locked(
            session,
            task,
            block=block,
            account_key=account_key,
            notify=notify,
            _depth=_depth,
        )
    else:
        async with _schedule_lock:
            planned = await _schedule_task_locked(
                session,
                task,
                block=block,
                account_key=account_key,
                notify=notify,
                _depth=_depth,
            )
    return (planned.start, planned.end) if planned is not None else None


async def _schedule_task_locked(
    session: Session,
    task: Task,
    *,
    block: Interval | None,
    account_key: str | None,
    notify: bool,
    _depth: int,
) -> PlannedSlot | None:
    is_fresh = task.calendar_event_id is None
    prior = scheduled_interval_for(task)

    try:
        planned = await plan_task_slot(
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
            await update_task_event(session, task, start=planned.start, end=planned.end)
        else:
            await add_task_event(session, task, start=planned.start, end=planned.end)
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

    # Each placement→replan→displaced-task hop increments _depth, so a
    # replan cycle (A's legs move B, B's legs move A, …) terminates here
    # instead of ping-ponging forever.
    if _depth < MAX_REPAIR_DEPTH:
        await _replan_commutes_around(
            session,
            task,
            planned=planned,
            prior=prior,
            account_key=account_key,
            _depth=_depth,
        )
    else:
        log.warning(
            "plan.schedule · replan depth limit reached task=%s; skipping commute replan",
            task.id,
        )

    if notify:
        if is_fresh:
            # First slot for this task — announce it. Shares the task tag, so
            # it also replaces any lingering "could not schedule" warning.
            from app.services.notify import notify_task_created

            await notify_task_created(task, start=planned.start, end=planned.end)
        else:
            # A silent reschedule of an already-mirrored task. Still clear a
            # possible warning so a recovered task doesn't keep nagging.
            from app.services.notify import clear_task_notification

            await clear_task_notification(task.id)

        # The trip's legs are failed placeholders — Maps found no route.
        # Own tag, so the "Scheduled" notification above doesn't swallow
        # the warning.
        if planned.unroutable:
            location = _trip_candidate_location(task, get_settings())
            if location is not None:
                from app.services.notify import notify_unroutable_location

                await notify_unroutable_location(task, location)
    return planned


async def reschedule_event(
    session: Session,
    event_id: str,
    *,
    account_key: str | None = None,
) -> None:
    """Dispatch a calendar-discover overlap.

    If the event maps to a Task, re-plan that task. Otherwise treat it as a
    managed commute leg and re-derive the legs around its anchors — legs are
    never repaired in place.
    """
    task = _task_for_event(session, event_id)
    if task is not None:
        result = await schedule_task(session, task, account_key=account_key)
        if result is not None:
            from app.events import publish_task

            publish_task(session, task.id)
        return
    await _replan_window_for_commute(session, event_id, account_key=account_key)


async def plan_task_slot(
    session: Session,
    task: Task,
    *,
    extra_busy: list[Interval] | None = None,
    block: Interval | None = None,
    account_key: str | None = None,
    _depth: int = 0,
) -> PlannedSlot:
    """Return a `PlannedSlot` for `task` (task span + reserved trip block).

    Located tasks try piggyback and chain-insert placements first, then a
    standalone sweep for the whole trip block. The free search scans days
    forward from `max(now, due - 7d)`; within each day it starts at the
    target time and moves backward toward the start time. If the normal
    10:00–20:00 window cannot place the block, the planner automatically
    tries the extended 08:00–24:00 fallback before raising.
    """
    if task.due_date is None:
        raise ValueError("task has no due_date")

    settings = get_settings()
    due = to_user_tz(task.due_date)
    now = datetime.now(user_tz()) + timedelta(minutes=settings.slot_min_lead_minutes)
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
    out_s, in_s, unroutable = await _estimate_trip_legs(session, task, reference=window_end)

    def _finalize(ps: PlannedSlot) -> PlannedSlot:
        return replace(ps, unroutable=True) if unroutable else ps

    if out_s or in_s:
        location = resolve_location_alias(task.location)
        planned = _piggyback_slot(
            busy,
            location=location,
            duration=duration,
            buffer=buffer,
            out_s=out_s,
            in_s=in_s,
            window_start=window_start,
            window_end=window_end,
        )
        if planned is None and not unroutable:
            planned = await _chain_insert_slot(
                session,
                busy,
                location=location,
                duration=duration,
                buffer=buffer,
                window_start=window_start,
                window_end=window_end,
            )
        if planned is not None:
            return _finalize(planned)

    planned = await _swept_block(
        session, task, busy,
        duration=duration, buffer=buffer, out_s=out_s, in_s=in_s,
        window_start=window_start, window_end=window_end, extended_window=False,
    )
    if planned is not None:
        return _finalize(planned)

    try:
        return _finalize(await _repair_by_displacing_task(
            session,
            task,
            busy,
            duration=duration,
            buffer=buffer,
            out_s=out_s,
            in_s=in_s,
            window_start=window_start,
            window_end=window_end,
            account_key=account_key,
            depth=_depth,
            extended_window=False,
        ))
    except ValueError:
        log.info(
            "plan.schedule · normal window exhausted task=%s due=%s; trying extended window",
            task.id, task.due_date.isoformat() if task.due_date else None,
        )

    planned = await _swept_block(
        session, task, busy,
        duration=duration, buffer=buffer, out_s=out_s, in_s=in_s,
        window_start=window_start, window_end=window_end, extended_window=True,
    )
    if planned is not None:
        return _finalize(planned)

    return _finalize(await _repair_by_displacing_task(
        session,
        task,
        busy,
        duration=duration,
        buffer=buffer,
        out_s=out_s,
        in_s=in_s,
        window_start=window_start,
        window_end=window_end,
        account_key=account_key,
        depth=_depth,
        extended_window=True,
    ))


# --- Trip-block geometry -------------------------------------------------


def _block_total(duration: timedelta, buffer: timedelta, out_s: int, in_s: int) -> timedelta:
    total = duration
    if out_s:
        total += timedelta(seconds=out_s) + buffer
    if in_s:
        total += timedelta(seconds=in_s) + buffer
    return total


def _planned_from_block(
    slot: tuple[datetime, datetime],
    duration: timedelta,
    buffer: timedelta,
    out_s: int,
    in_s: int,
) -> PlannedSlot:
    block_start, block_end = slot
    start = block_start + (timedelta(seconds=out_s) + buffer if out_s else timedelta(0))
    return PlannedSlot(start, start + duration, block_start, block_end, out_s, in_s)


async def _swept_block(
    session: Session,
    task: Task,
    busy: list[BusyEvent],
    *,
    duration: timedelta,
    buffer: timedelta,
    out_s: int,
    in_s: int,
    window_start: datetime,
    window_end: datetime,
    extended_window: bool,
) -> PlannedSlot | None:
    """Sweep for the whole trip block; leg estimates are re-resolved at the
    found slot's actual hour and the sweep retried once if they grew."""
    for _ in range(2):
        total = _block_total(duration, buffer, out_s, in_s)
        slot = _find_free_slot(
            busy, total, window_start, window_end, buffer, extended_window=extended_window,
        )
        if slot is None:
            return None
        planned = _planned_from_block(slot, duration, buffer, out_s, in_s)
        if not (out_s or in_s):
            return planned
        new_out, new_in, _ = await _estimate_trip_legs(session, task, reference=planned.block_start)
        if new_out <= out_s and new_in <= in_s:
            return planned
        out_s, in_s = max(out_s, new_out), max(in_s, new_in)
    return planned


def _piggyback_slot(
    busy: list[BusyEvent],
    *,
    location: str,
    duration: timedelta,
    buffer: timedelta,
    out_s: int,
    in_s: int,
    window_start: datetime,
    window_end: datetime,
) -> PlannedSlot | None:
    """Slot touching an anchor at the task's own location — zero marginal
    commute. The anchor's own leg on the shared side is ignored during the
    conflict check: it gets re-derived around the task."""
    target = _norm_loc(location)
    anchors = [
        ev for ev in busy
        if ev.kind != "commute" and ev.location and _norm_loc(ev.location) == target
    ]
    for anchor in sorted(anchors, key=lambda ev: ev.start, reverse=True):
        start = anchor.end + buffer
        end = start + duration
        block_end = end + buffer + timedelta(seconds=in_s) if in_s else end
        if (
            start >= window_start
            and end <= window_end
            and _within_day_window(start, end, extended=False)
            and _conflict_free(busy, start, block_end, ignore_anchor_leg=(anchor.id, 0))
        ):
            return PlannedSlot(start, end, start, block_end, 0, in_s)

        end = anchor.start - buffer
        start = end - duration
        block_start = start - buffer - timedelta(seconds=out_s) if out_s else start
        if (
            start >= window_start
            and end <= window_end
            and _within_day_window(start, end, extended=False)
            and _conflict_free(busy, block_start, end, ignore_anchor_leg=(anchor.id, 1))
        ):
            return PlannedSlot(start, end, block_start, end, out_s, 0)
    return None


async def _chain_insert_slot(
    session: Session,
    busy: list[BusyEvent],
    *,
    location: str,
    duration: timedelta,
    buffer: timedelta,
    window_start: datetime,
    window_end: datetime,
) -> PlannedSlot | None:
    """Insert the task into a gap between two located anchors when the detour
    travel fits — the user is already out, so day-window bounds don't apply.
    Picks the gap with the least added travel."""
    target = _norm_loc(location)
    anchors = sorted(
        (ev for ev in busy if ev.kind != "commute" and ev.location),
        key=lambda ev: ev.start,
    )
    gaps = [
        (prev, nxt)
        for prev, nxt in zip(anchors, anchors[1:], strict=False)
        if nxt.start - prev.end >= duration + 4 * buffer
        and prev.end >= window_start
        and prev.end + duration <= window_end
        and _norm_loc(prev.location) != target
        and _norm_loc(nxt.location) != target
    ]
    gaps.sort(key=lambda pair: pair[1].start, reverse=True)

    best: tuple[int, PlannedSlot] | None = None
    for prev, nxt in gaps[:MAX_CHAIN_GAPS]:
        t_pt = await _one_way_seconds(session, prev.location, location, prev.end)
        t_tn = await _one_way_seconds(session, location, nxt.location, nxt.start)
        if t_pt is None or t_tn is None:
            continue
        need = timedelta(seconds=t_pt + t_tn) + duration + 4 * buffer
        if nxt.start - prev.end < need:
            continue
        end = min(nxt.start - 2 * buffer - timedelta(seconds=t_tn), window_end)
        start = end - duration
        if start - 2 * buffer - timedelta(seconds=t_pt) < prev.end or start < window_start:
            continue
        block_start = start - buffer - timedelta(seconds=t_pt)
        block_end = end + buffer + timedelta(seconds=t_tn)
        if not _conflict_free(
            busy, block_start, block_end,
            ignore_anchor_leg=(prev.id, 0), ignore_anchor_leg2=(nxt.id, 1),
        ):
            continue
        t_pn = await _one_way_seconds(session, prev.location, nxt.location, prev.end) or 0
        added = t_pt + t_tn - t_pn
        planned = PlannedSlot(start, end, block_start, block_end, t_pt, t_tn)
        if best is None or added < best[0]:
            best = (added, planned)
    return best[1] if best else None


def _conflict_free(
    busy: list[BusyEvent],
    start: datetime,
    end: datetime,
    *,
    ignore_anchor_leg: tuple[str, int] | None = None,
    ignore_anchor_leg2: tuple[str, int] | None = None,
) -> bool:
    """No busy event overlaps `[start, end)`. `ignore_anchor_leg=(anchor_id,
    side)` skips the commute leg leaving (side 0) or entering (side 1) that
    anchor — it will be re-derived around the candidate."""
    for ev in busy:
        if ev.leg_key is not None:
            if ignore_anchor_leg and ev.leg_key[ignore_anchor_leg[1]] == ignore_anchor_leg[0]:
                continue
            if ignore_anchor_leg2 and ev.leg_key[ignore_anchor_leg2[1]] == ignore_anchor_leg2[0]:
                continue
        if ev.start < end and start < ev.end:
            return False
    return True


def _within_day_window(start: datetime, end: datetime, *, extended: bool) -> bool:
    if start.date() != end.date():
        return False
    lower, upper = (EARLY_MORNING, END_OF_DAY) if extended else (DAY_START, DAY_TARGET)
    return lower <= start.time() and end.time() <= upper


# --- Leg estimation -------------------------------------------------------


async def _estimate_trip_legs(
    session: Session,
    task: Task,
    *,
    reference: datetime,
) -> tuple[int, int, bool]:
    """Worst-case standalone `(outbound, inbound, unroutable)` for the task's
    trip block. Zero legs for location-less / online / at-home tasks; a leg
    Maps can't route reserves the 30-minute failed placeholder and flips
    `unroutable`. Rain is deliberately ignored here — the reservation is
    mode-optimistic and the hourly weather refresh reconciles flips."""
    settings = get_settings()
    location = _trip_candidate_location(task, settings)
    if location is None:
        return 0, 0, False
    home = settings.home_address.strip()
    out = await _one_way_seconds(session, home, location, reference)
    inbound = await _one_way_seconds(session, location, home, reference)
    unroutable = out is None or inbound is None
    return (
        out if out is not None else FAILED_LEG_SECONDS,
        inbound if inbound is not None else FAILED_LEG_SECONDS,
        unroutable,
    )


def _trip_candidate_location(task, settings) -> str | None:
    """The routable address a trip block must reach, or None when no commute
    applies (feature off, no/online location, or the task is at home)."""
    if not settings.commute_enabled or not settings.google_maps_api_key:
        return None
    home = (settings.home_address or "").strip()
    location = resolve_location_alias(task.location)
    if not home or not location or is_online_location(location):
        return None
    if _norm_loc(location) == _norm_loc(home):
        return None
    return location


async def _one_way_seconds(
    session: Session,
    origin: str,
    destination: str,
    reference: datetime,
) -> int | None:
    """Duration for one leg using the planner's mode rule (bike if it fits
    the threshold, else transit, else whatever routes). None if unroutable."""
    from app.services.commute.resolver import resolve_duration

    settings = get_settings()
    try:
        bike = await resolve_duration(
            session, origin=origin, destination=destination,
            mode="bicycling", departure=reference,
        )
    except Exception as exc:  # noqa: BLE001 — Maps hiccups must not kill planning
        log.warning("plan.schedule · bike lookup failed %s->%s err=%s", origin, destination, exc)
        bike = None
    if bike is not None and bike <= settings.commute_bike_max_minutes * 60:
        return bike
    try:
        transit = await resolve_duration(
            session, origin=origin, destination=destination,
            mode="transit", departure=reference,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.schedule · transit lookup failed %s->%s err=%s", origin, destination, exc)
        transit = None
    return transit if transit is not None else bike


def _cached_trip_legs(
    session: Session,
    task,
    settings,
) -> tuple[int, int]:
    """Cache-only leg estimate for the DB busy backstop — never hits Maps."""
    location = _trip_candidate_location(task, settings)
    if location is None:
        return 0, 0
    home = settings.home_address.strip()
    from app.db.clients import route_cache

    def cached(origin: str, destination: str) -> int:
        # Bike routes live in the time-invariant bucket 0 (see resolver).
        row = route_cache.lookup_with_bicycling_reverse(
            session, origin=origin, destination=destination,
            mode="bicycling", hour_bucket=0,
        )
        return row.duration_seconds if row is not None else 0

    return cached(home, location), cached(location, home)


def _norm_loc(location: str | None) -> str:
    return " ".join((location or "").lower().split())


# --- Free-slot sweeps -----------------------------------------------------


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


# --- Displacement repair ---------------------------------------------------


async def _repair_by_displacing_task(
    session: Session,
    task: Task,
    busy: list[BusyEvent],
    *,
    duration: timedelta,
    buffer: timedelta,
    out_s: int,
    in_s: int,
    window_start: datetime,
    window_end: datetime,
    account_key: str | None,
    depth: int,
    extended_window: bool,
) -> PlannedSlot:
    if depth >= MAX_REPAIR_DEPTH:
        raise ValueError("repair recursion limit reached")

    total = _block_total(duration, buffer, out_s, in_s)
    victims = _movable_victims(
        session, task, busy, total, buffer, window_start, window_end,
    )
    for victim, victim_event, freed_range in victims:
        block = Interval(victim_event.start, victim_event.end, victim_event.id)
        try:
            victim_planned = await _schedule_task_locked(
                session,
                victim,
                block=block,
                account_key=account_key,
                notify=True,
                _depth=depth + 1,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan.schedule · victim move failed task=%s err=%s", victim.id, exc)
            continue
        if victim_planned is None:
            # Victim couldn't be rescheduled — its old slot still holds.
            continue

        # Rebuild the busy view: the victim (and its legs — they move with
        # it) vacated, and its new trip block is occupied. Then sweep the
        # freed range (clamped to the per-day working window) for the new
        # task's block.
        new_busy = [
            ev for ev in busy
            if ev.id != victim_event.id
            and not (ev.leg_key is not None and victim_event.id in ev.leg_key)
        ]
        new_busy.append(
            BusyEvent(
                victim_event.id,
                victim_planned.block_start,
                victim_planned.block_end,
                "busy",
            )
        )
        slot = _slot_in_range(
            new_busy,
            total,
            buffer,
            freed_range,
            extended_window=extended_window,
        )
        if slot is not None:
            return _planned_from_block(slot, duration, buffer, out_s, in_s)

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
    # Location-less victims first (moving a located task breaks its trip
    # chain and forces leg rework), then least-urgent (latest due) first.
    rows.sort(
        key=lambda row: (
            1 if (row.location or "").strip() else 0,
            -(row.due_date.timestamp() if row.due_date else 0),
        )
    )
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
    victim, falling back to the planning window edges when none exist. The
    victim's own commute legs vacate with it, so they don't bound the range.
    """
    prev_end = window_start
    next_start = window_end
    for ev in busy:
        if ev.id == victim.id:
            continue
        if ev.leg_key is not None and victim.id in ev.leg_key:
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


# --- Busy view -------------------------------------------------------------


def _db_scheduled_busy(
    session: Session,
    task: Task,
    window_start: datetime,
    window_end: datetime,
    calendar_event_ids: set[str],
) -> list[BusyEvent]:
    """Backstop for the live calendar read: open tasks whose stored slot the
    calendar may not reflect yet — Google's events.list lags a just-created
    event, or the event was deleted out from under us. Located tasks are
    inflated by their cached leg durations so the backstop reserves the trip,
    not just the task. Skip any whose event already appeared in the calendar
    busy set. Marked immovable ("busy"): we won't try to displace a task we
    can only see in the DB."""
    from app.db.clients import tasks as tasks_store

    settings = get_settings()
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
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
        out_s, in_s = _cached_trip_legs(session, row, settings)
        if out_s:
            start -= buffer + timedelta(seconds=out_s)
        if in_s:
            end += buffer + timedelta(seconds=in_s)
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
    from app.services.calendar import (
        commute_leg_key,
        is_commute_event,
        is_task_event,
        list_events_between,
    )

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
        location = resolve_location_alias(ev.location)
        out.append(
            BusyEvent(
                ev.id,
                to_user_tz(ev.start),
                to_user_tz(ev.end),
                kind,
                location=location if location and not is_online_location(location) else None,
                leg_key=commute_leg_key(ev),
            )
        )
    return out


# --- Commute replans --------------------------------------------------------


async def _replan_window_for_commute(
    session: Session,
    event_id: str,
    *,
    account_key: str | None,
) -> None:
    """Discover found an overlap with a managed commute leg: re-derive the
    legs around its anchors instead of repairing the leg in place."""
    if not get_settings().commute_enabled:
        return
    from app.services.calendar import get_event, is_commute_event

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
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    margin = commute_window_margin()
    await plan_commutes_window_best_effort(
        session,
        window_start=event.start - margin,
        window_end=event.end + margin,
        account_key=account_key,
    )


async def _replan_commutes_around(
    session: Session,
    task: Task,
    *,
    planned: PlannedSlot,
    prior: Interval | None,
    account_key: str | None,
    _depth: int,
) -> None:
    """Re-derive commute legs over the window a placement touched: the new
    trip block plus the vacated prior slot, so stale legs there are removed
    and the old neighbours reconnect."""
    if not get_settings().commute_enabled or not task.calendar_event_id:
        return
    location = resolve_location_alias(task.location)
    if not location or is_online_location(location):
        return
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    window_start = planned.block_start
    window_end = planned.block_end
    if prior is not None:
        window_start = min(window_start, prior.start)
        window_end = max(window_end, prior.end)
    margin = commute_window_margin()
    await plan_commutes_window_best_effort(
        session,
        window_start=window_start - margin,
        window_end=window_end + margin,
        account_key=account_key,
        _depth=_depth + 1,
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
