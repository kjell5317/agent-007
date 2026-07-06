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
   b. only if none exists before the deadline, move a managed task out of
      the way — least urgent (latest due date) first — and retry the
      placements above against the thinned calendar. A victim that has no
      free slot itself displaces the next victim recursively, bounded by
      `MAX_REPAIR_DEPTH` and the shared displacement ledger.

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
from app.services.location import (
    is_online_location,
    resolve_location_alias,
    resolve_routable_location,
    resolve_routable_location_sync,
)
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

LEAD_DAYS = 7
DAY_START = time(10, 0)
DAY_TARGET = time(21, 0)
# Extended-mode bounds. The normal `[DAY_START, DAY_TARGET]` range was
# already exhausted on the first attempt that triggered the "no slot"
# notification, so extended mode reaches past it — but a block forced into
# the extension should sit in it as little as possible. Each day is scanned
# in two phases, both allowed to straddle back across the normal-window edge:
#   1. late evening — forward from DAY_START to END_OF_DAY (10→24): the
#      earliest fit crosses DAY_TARGET with only its tail past 21:00.
#   2. early morning — backward from DAY_TARGET to EARLY_MORNING (21→8): the
#      latest fit crosses DAY_START with only its head before 10:00
#      (an 8-10 block becomes 9-11 when 10-11 is free).
# Sweeping from the far edge is what minimises the overlap; a normal-only
# slot never surfaces here — it would have been placed on the first attempt.
# Then the next day, same procedure.
EARLY_MORNING = time(8, 0)
END_OF_DAY = time(23, 59, 59)
MAX_REPAIR_DEPTH = 8
# Chain-insert evaluates at most this many gaps (each costs Maps lookups).
MAX_CHAIN_GAPS = 8
# Displacement repair tries at most this many victims per attempt — each one
# costs a full nested planning round (calendar lists + sweeps).
MAX_VICTIM_ATTEMPTS = 5

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
class Gaps:
    """Minimum gaps a placement keeps: `commute` only between a leg and its
    own anchor (the trip's inner boundaries), `event` everywhere else — a
    leg keeps the full event gap to any event it doesn't serve, so
    `task -15-> leg -5-> task -5-> leg -15-> task` holds."""

    commute: timedelta
    event: timedelta

    def neighbor_gap(
        self,
        ev: BusyEvent,
        inner_ids: set[str] = frozenset(),
    ) -> timedelta:
        """Gap required between a candidate placement and `ev`.

        A "block" marker is the task's own vacated slot, not a real event —
        only actual overlap is forbidden, so a task may land flush against
        where it just was. Anchors the candidate chains with (`inner_ids`)
        get the inner commute gap; everything else the event gap."""
        if ev.kind == "block":
            return timedelta(0)
        if ev.id in inner_ids:
            return self.commute
        return self.event


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
    primary_action: dict[str, str] | None = None,
    _depth: int = 0,
    _displaced: set | None = None,
) -> tuple[datetime, datetime] | None:
    """Plan `task` and create/update its calendar mirror (plus commute legs).

    The planner searches the normal 10:00–21:00 window first. If that
    cannot place the task, it automatically tries the extended 08:00–24:00
    fallback before reporting no slot.

    `notify=False` suppresses the per-task scheduled/no-slot notification —
    batch callers (commute reschedule of many tasks) handle their own
    aggregated notification. `primary_action` is forwarded opaquely to the
    "Scheduled" notification to swap its leading button (kotx tasks use this).

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
            primary_action=primary_action,
            _depth=_depth,
            _displaced=_displaced,
        )
    else:
        async with _schedule_lock:
            planned = await _schedule_task_locked(
                session,
                task,
                block=block,
                account_key=account_key,
                notify=notify,
                primary_action=primary_action,
                _depth=_depth,
                _displaced=_displaced,
            )
    return (planned.start, planned.end) if planned is not None else None


async def _schedule_task_locked(
    session: Session,
    task: Task,
    *,
    block: Interval | None,
    account_key: str | None,
    notify: bool,
    primary_action: dict[str, str] | None = None,
    _depth: int,
    _displaced: set | None = None,
    _keep_slot: bool = False,
    _busy_snapshot: list[BusyEvent] | None = None,
    _snapshot_range: tuple[datetime, datetime] | None = None,
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
            _displaced=_displaced,
            _busy_snapshot=_busy_snapshot,
            _snapshot_range=_snapshot_range,
        )
    except ValueError:
        log.warning(
            "plan.schedule · no slot for task=%s due=%s",
            task.id,
            task.due_date.isoformat() if task.due_date else None,
        )
        if prior is not None and not _keep_slot:
            # No valid slot exists for this task anymore — a stale
            # scheduled_date (past or future-but-conflicting) shows a
            # schedule that isn't real and breaks the frontend's
            # unscheduled indicators, and discover would keep syncing it
            # back from the mirror. Drop both; the cron retry sweep and
            # discover changes pick the task up again. Displacement victims
            # opt out (`_keep_slot`): their slot is still valid — they were
            # only probed to make room for someone else.
            log.info("plan.schedule · clearing unschedulable slot task=%s", task.id)
            from app.services.calendar import delete_task_event

            try:
                await delete_task_event(session, task)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "plan.schedule · stale mirror delete failed task=%s err=%s", task.id, exc,
                )
            task.calendar_event_id = None
            task.scheduled_date = None
            session.flush()
            session.commit()
            from app.events import publish_task

            publish_task(session, task.id)
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
            task.id,
            exc,
            exc_info=True,
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

            await notify_task_created(
                task, start=planned.start, end=planned.end, primary_action=primary_action
            )
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
    _displaced: set | None = None,
    _busy_snapshot: list[BusyEvent] | None = None,
    _snapshot_range: tuple[datetime, datetime] | None = None,
) -> PlannedSlot:
    """Return a `PlannedSlot` for `task` (task span + reserved trip block).

    Located tasks try piggyback and chain-insert placements first, then a
    standalone sweep for the whole trip block. The free search scans days
    forward from `max(now, due - 7d)`; within each day it starts at the
    target time and moves backward toward the start time. If the normal
    10:00–21:00 window cannot place the block, the planner automatically
    tries the extended 08:00–24:00 fallback before raising.

    `_busy_snapshot` / `_snapshot_range` carry the parent's raw calendar read
    down a displacement chain: a victim reschedule whose window nests inside
    the range reuses it instead of hitting the Calendar API again. Freshness
    is preserved because our own in-chain writes are reconciled from the DB
    (`_db_scheduled_busy`) and the not-yet-written slot arrives via `block` —
    and nothing external mutates the calendar mid-chain (one locked loop).
    """
    if task.due_date is None:
        raise ValueError("task has no due_date")

    # One ledger per top-level scheduling call, shared (same object) with
    # every nested victim attempt: a task is planned at most once per call,
    # which keeps displacement repair linear instead of exponential.
    displaced = _displaced if _displaced is not None else set()

    settings = get_settings()
    due = to_user_tz(task.due_date)
    now = datetime.now(user_tz()) + timedelta(minutes=settings.slot_min_lead_minutes)
    window_start = max(now, due - timedelta(days=LEAD_DAYS))
    window_end = due
    if window_end <= window_start:
        raise ValueError("deadline is in the past")

    gaps = Gaps(
        commute=timedelta(minutes=settings.commute_event_buffer_minutes),
        event=timedelta(minutes=settings.event_buffer_minutes),
    )
    pad = max(gaps.commute, gaps.event)
    # Fetch one gap beyond the window on both sides: conflict probes reach
    # a gap past a candidate slot, and Google's timeMax is exclusive on the
    # event *start* — without the pad, an event starting exactly at the due
    # date is invisible and the task lands flush against it.
    fetch_lo, fetch_hi = window_start - pad, window_end + pad
    if (
        _busy_snapshot is not None
        and _snapshot_range is not None
        and _snapshot_range[0] <= fetch_lo
        and fetch_hi <= _snapshot_range[1]
    ):
        raw_busy, covered = _busy_snapshot, _snapshot_range
    else:
        raw_busy = await _fetch_busy_events(session, fetch_lo, fetch_hi, account_key=account_key)
        covered = (fetch_lo, fetch_hi)
    # The raw snapshot is exclusion-agnostic so a reusing victim can drop its
    # own mirror; the parent's mirror stays visible to it (unwritten move).
    busy = [ev for ev in raw_busy if ev.id != task.calendar_event_id]
    busy = _db_scheduled_busy(session, task, fetch_lo, fetch_hi, busy)
    for itv in extra_busy or []:
        busy.append(BusyEvent(itv.event_id or "extra", itv.start, itv.end, "extra"))
    if block is not None:
        busy.append(BusyEvent(block.event_id or "block", block.start, block.end, "block"))

    duration = timedelta(minutes=_duration_minutes(task, settings))
    out_s, in_s, unroutable = await _estimate_trip_legs(session, task, reference=window_end)

    def _finalize(ps: PlannedSlot) -> PlannedSlot:
        # Every placement path funnels through here — a last-line invariant so
        # no path can ever write a task outside the widest working window.
        if not _within_day_window(ps.start, ps.end, extended=True):
            log.error(
                "plan.schedule · placement %s–%s escaped the day window for task=%s; rejecting",
                ps.start.isoformat(),
                ps.end.isoformat(),
                task.id,
            )
            raise ValueError("placement outside day window")
        return replace(ps, unroutable=True) if unroutable else ps

    location: str | None = None
    if out_s or in_s:
        max_wait = timedelta(minutes=settings.commute_home_layover_minutes)
        location = await _routable_trip_candidate_location(task, settings)
        if location is None:
            location = resolve_location_alias(task.location)
        planned = _piggyback_slot(
            busy,
            location=location,
            duration=duration,
            gaps=gaps,
            out_s=out_s,
            in_s=in_s,
            window_start=window_start,
            window_end=window_end,
            max_wait=max_wait,
        )
        if planned is None and not unroutable:
            planned = await _chain_insert_slot(
                session,
                busy,
                location=location,
                duration=duration,
                gaps=gaps,
                window_start=window_start,
                window_end=window_end,
            )
        if planned is not None:
            return _finalize(planned)

    planned = await _swept_block(
        session,
        task,
        busy,
        duration=duration,
        gaps=gaps,
        out_s=out_s,
        in_s=in_s,
        window_start=window_start,
        window_end=window_end,
        extended_window=False,
    )
    if planned is not None:
        return _finalize(planned)

    try:
        return _finalize(
            await _repair_by_displacing_task(
                session,
                task,
                busy,
                location=location,
                duration=duration,
                gaps=gaps,
                out_s=out_s,
                in_s=in_s,
                unroutable=unroutable,
                window_start=window_start,
                window_end=window_end,
                account_key=account_key,
                depth=_depth,
                displaced=displaced,
                extended_window=False,
                busy_snapshot=raw_busy,
                snapshot_range=covered,
            )
        )
    except ValueError:
        log.info(
            "plan.schedule · normal window exhausted task=%s due=%s; trying extended window",
            task.id,
            task.due_date.isoformat() if task.due_date else None,
        )

    if out_s or in_s:
        planned = _piggyback_slot(
            busy,
            location=location,
            duration=duration,
            gaps=gaps,
            out_s=out_s,
            in_s=in_s,
            window_start=window_start,
            window_end=window_end,
            extended=True,
            max_wait=timedelta(minutes=settings.commute_home_layover_minutes),
        )
        if planned is not None:
            return _finalize(planned)

    planned = await _swept_block(
        session,
        task,
        busy,
        duration=duration,
        gaps=gaps,
        out_s=out_s,
        in_s=in_s,
        window_start=window_start,
        window_end=window_end,
        extended_window=True,
    )
    if planned is not None:
        return _finalize(planned)

    return _finalize(
        await _repair_by_displacing_task(
            session,
            task,
            busy,
            location=location,
            duration=duration,
            gaps=gaps,
            out_s=out_s,
            in_s=in_s,
            unroutable=unroutable,
            window_start=window_start,
            window_end=window_end,
            account_key=account_key,
            depth=_depth,
            displaced=displaced,
            extended_window=True,
            busy_snapshot=raw_busy,
            snapshot_range=covered,
        )
    )


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
    gaps: Gaps,
    out_s: int,
    in_s: int,
    window_start: datetime,
    window_end: datetime,
    extended_window: bool,
) -> PlannedSlot | None:
    """Sweep for the whole trip block; leg estimates are re-resolved at the
    found slot's actual hour and the sweep retried once if they grew."""
    for _ in range(2):
        total = _block_total(duration, gaps.commute, out_s, in_s)
        slot = _find_free_slot(
            busy,
            total,
            window_start,
            window_end,
            gaps,
            extended_window=extended_window,
        )
        if slot is None:
            return None
        planned = _planned_from_block(slot, duration, gaps.commute, out_s, in_s)
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
    gaps: Gaps,
    out_s: int,
    in_s: int,
    window_start: datetime,
    window_end: datetime,
    extended: bool = False,
    max_wait: timedelta = timedelta(minutes=60),
) -> PlannedSlot | None:
    """Slot next to an anchor at the task's own location — zero marginal
    commute on the shared side. The candidate slides away from the anchor
    past conflicts (e.g. an online meeting attended in place) as long as
    the wait stays under `max_wait` (beyond the home-layover threshold a
    round trip home wins, which the standalone sweep covers) and no
    differently-located anchor implies having left. The anchor's own leg
    on the shared side is ignored during the conflict check: it gets
    re-derived around the task."""
    target = _norm_loc(location)
    located = [ev for ev in busy if ev.kind != "commute" and ev.location]
    anchors = [ev for ev in located if _norm_loc(ev.location) == target]
    elsewhere = [ev for ev in located if _norm_loc(ev.location) != target]
    for anchor in sorted(anchors, key=lambda ev: ev.start, reverse=True):
        planned = _piggyback_after(
            anchor, busy, elsewhere,
            duration=duration, gaps=gaps, in_s=in_s,
            window_start=window_start, window_end=window_end,
            extended=extended, max_wait=max_wait,
        )
        if planned is not None:
            return planned
        planned = _piggyback_before(
            anchor, busy, elsewhere,
            duration=duration, gaps=gaps, out_s=out_s,
            window_start=window_start, window_end=window_end,
            extended=extended, max_wait=max_wait,
        )
        if planned is not None:
            return planned
    return None


def _piggyback_after(
    anchor: BusyEvent,
    busy: list[BusyEvent],
    elsewhere: list[BusyEvent],
    *,
    duration: timedelta,
    gaps: Gaps,
    in_s: int,
    window_start: datetime,
    window_end: datetime,
    extended: bool,
    max_wait: timedelta,
) -> PlannedSlot | None:
    # The user leaves for the next differently-located anchor — the
    # zero-outbound assumption only holds until then.
    leave = min((ev.start for ev in elsewhere if ev.start >= anchor.end), default=None)
    latest_start = anchor.end + max_wait
    cursor = max(anchor.end + gaps.event, window_start)
    while cursor <= latest_start:
        start, end = cursor, cursor + duration
        if end > window_end or (leave is not None and end > leave):
            return None
        block_end = end + gaps.commute + timedelta(seconds=in_s) if in_s else end
        conflicts = _gap_conflicts(
            busy, start, block_end, gaps, ignore_anchor_leg=(anchor.id, 0),
        )
        if not conflicts:
            if _within_day_window(start, end, extended=extended):
                return PlannedSlot(start, end, start, block_end, 0, in_s)
            lower = EARLY_MORNING if extended else DAY_START
            if start.time() < lower and start.date() == end.date():
                cursor = datetime.combine(start.date(), lower, tzinfo=start.tzinfo)
                continue
            # Free but past the day's upper bound — later only gets worse.
            return None
        cursor = max(ev.end + gaps.neighbor_gap(ev) for ev in conflicts)
    return None


def _piggyback_before(
    anchor: BusyEvent,
    busy: list[BusyEvent],
    elsewhere: list[BusyEvent],
    *,
    duration: timedelta,
    gaps: Gaps,
    out_s: int,
    window_start: datetime,
    window_end: datetime,
    extended: bool,
    max_wait: timedelta,
) -> PlannedSlot | None:
    # The user arrives from the previous differently-located anchor — the
    # stays-until-the-anchor assumption only holds back to there.
    came_from = max((ev.end for ev in elsewhere if ev.end <= anchor.start), default=None)
    earliest_end = anchor.start - max_wait
    cursor = min(anchor.start - gaps.event, window_end)
    while cursor >= earliest_end:
        end, start = cursor, cursor - duration
        if start < window_start or (came_from is not None and end < came_from):
            return None
        block_start = start - gaps.commute - timedelta(seconds=out_s) if out_s else start
        conflicts = _gap_conflicts(
            busy, block_start, end, gaps, ignore_anchor_leg=(anchor.id, 1),
        )
        if not conflicts:
            if _within_day_window(start, end, extended=extended):
                return PlannedSlot(start, end, block_start, end, out_s, 0)
            upper = END_OF_DAY if extended else DAY_TARGET
            if end.time() > upper and start.date() == end.date():
                cursor = datetime.combine(end.date(), upper, tzinfo=end.tzinfo)
                continue
            # Free but before the day's lower bound — earlier only gets worse.
            return None
        cursor = min(ev.start - gaps.neighbor_gap(ev) for ev in conflicts)
    return None


async def _chain_insert_slot(
    session: Session,
    busy: list[BusyEvent],
    *,
    location: str,
    duration: timedelta,
    gaps: Gaps,
    window_start: datetime,
    window_end: datetime,
    extended: bool = False,
) -> PlannedSlot | None:
    """Insert the task into a gap between two located anchors when the detour
    travel fits. Being already out only waives a *fresh* commute, not the
    working day: the task itself must still land inside the active window
    (`extended` picks normal 10–21 vs. 08–24), or a gap between two late/early
    anchors would schedule work at 3am. Picks the gap with the least added
    travel. Every boundary here touches a leg (anchor↔leg, leg↔task), so all
    spacing uses the commute gap."""
    buffer = gaps.commute
    target = _norm_loc(location)
    anchors = sorted(
        (ev for ev in busy if ev.kind != "commute" and ev.location),
        key=lambda ev: ev.start,
    )
    candidates = [
        (prev, nxt)
        for prev, nxt in zip(anchors, anchors[1:], strict=False)
        if nxt.start - prev.end >= duration + 4 * buffer
        and prev.end >= window_start
        and prev.end + duration <= window_end
        and _norm_loc(prev.location) != target
        and _norm_loc(nxt.location) != target
    ]
    candidates.sort(key=lambda pair: pair[1].start, reverse=True)

    best: tuple[int, PlannedSlot] | None = None
    for prev, nxt in candidates[:MAX_CHAIN_GAPS]:
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
        if not _within_day_window(start, end, extended=extended):
            # On-the-way, but the gap sits outside the working day — a
            # standalone sweep will find an in-window slot instead.
            continue
        block_start = start - buffer - timedelta(seconds=t_pt)
        block_end = end + buffer + timedelta(seconds=t_tn)
        if not _conflict_free(
            busy,
            block_start,
            block_end,
            gaps=gaps,
            inner_ids={prev.id, nxt.id},
            ignore_anchor_leg=(prev.id, 0),
            ignore_anchor_leg2=(nxt.id, 1),
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
    gaps: Gaps,
    inner_ids: set[str] = frozenset(),
    ignore_anchor_leg: tuple[str, int] | None = None,
    ignore_anchor_leg2: tuple[str, int] | None = None,
) -> bool:
    """No busy event overlaps `[start, end)` or sits closer than its required
    gap. `inner_ids` are anchors the candidate block chains with directly —
    a leg edge faces them, so the inner commute gap applies. `ignore_anchor_leg
    =(anchor_id, side)` skips the commute leg leaving (side 0) or entering
    (side 1) that anchor — it will be re-derived around the candidate."""
    return not _gap_conflicts(
        busy,
        start,
        end,
        gaps,
        inner_ids=inner_ids,
        ignore_anchor_leg=ignore_anchor_leg,
        ignore_anchor_leg2=ignore_anchor_leg2,
    )


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
    location = await _routable_trip_candidate_location(task, settings)
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
    location = resolve_routable_location_sync(task.location)
    if not home or not location or is_online_location(location):
        return None
    if _norm_loc(location) == _norm_loc(home):
        return None
    return location


async def _routable_trip_candidate_location(task, settings) -> str | None:
    """Async trip target normalization for paths allowed to perform I/O."""
    if not settings.commute_enabled or not settings.google_maps_api_key:
        return None
    home = (settings.home_address or "").strip()
    location = await resolve_routable_location(task.location)
    if not home or not location:
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
            session,
            origin=origin,
            destination=destination,
            mode="bicycling",
            departure=reference,
        )
    except Exception as exc:  # noqa: BLE001 — Maps hiccups must not kill planning
        log.warning("plan.schedule · bike lookup failed %s->%s err=%s", origin, destination, exc)
        bike = None
    if bike is not None and bike <= settings.commute_bike_max_minutes * 60:
        return bike
    try:
        transit = await resolve_duration(
            session,
            origin=origin,
            destination=destination,
            mode="transit",
            departure=reference,
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
            session,
            origin=origin,
            destination=destination,
            mode="bicycling",
            hour_bucket=0,
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
    gaps: Gaps,
    *,
    extended_window: bool = False,
) -> tuple[datetime, datetime] | None:
    ordered = sorted(busy, key=lambda ev: ev.start)
    day = window_start.date()
    last_day = window_end.date()
    tz = user_tz()

    while day <= last_day:
        if extended_window:
            # Phase 1: late evening. Sweep up from DAY_START so the earliest
            # fit straddles down into the normal window, sitting past 21:00
            # only by its tail.
            lower, upper = _day_bounds(day, DAY_START, END_OF_DAY, tz, window_start, window_end)
            slot = _sweep_forward(ordered, duration, gaps, lower, upper)
            if slot is not None:
                return slot
            # Phase 2: early morning. Sweep down from DAY_TARGET so the latest
            # fit straddles up into the normal window, sitting before 10:00
            # only by its head.
            lower, upper = _day_bounds(day, EARLY_MORNING, DAY_TARGET, tz, window_start, window_end)
            slot = _sweep_backward(ordered, duration, gaps, lower, upper)
            if slot is not None:
                return slot
        else:
            lower, upper = _day_bounds(day, DAY_START, DAY_TARGET, tz, window_start, window_end)
            slot = _sweep_backward(ordered, duration, gaps, lower, upper)
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
    gaps: Gaps,
    lower: datetime,
    upper: datetime,
) -> tuple[datetime, datetime] | None:
    """Walk the cursor from `upper` down toward `lower`, looking for a
    `(cursor - duration, cursor)` slot that doesn't collide with `busy`."""
    cursor = upper
    while cursor - duration >= lower:
        start = cursor - duration
        end = cursor
        conflict = _latest_conflict(busy, start, end, gaps)
        if conflict is None:
            return start, end
        cursor = min(cursor, conflict.start - gaps.neighbor_gap(conflict))
    return None


def _sweep_forward(
    busy: list[BusyEvent],
    duration: timedelta,
    gaps: Gaps,
    lower: datetime,
    upper: datetime,
) -> tuple[datetime, datetime] | None:
    """Walk the cursor from `lower` up toward `upper`, looking for a
    `(cursor, cursor + duration)` slot that doesn't collide with `busy`."""
    cursor = lower
    while cursor + duration <= upper:
        start = cursor
        end = cursor + duration
        conflict = _earliest_conflict(busy, start, end, gaps)
        if conflict is None:
            return start, end
        cursor = max(cursor, conflict.end + gaps.neighbor_gap(conflict))
    return None


# --- Displacement repair ---------------------------------------------------


async def _repair_by_displacing_task(
    session: Session,
    task: Task,
    busy: list[BusyEvent],
    *,
    location: str | None,
    duration: timedelta,
    gaps: Gaps,
    out_s: int,
    in_s: int,
    unroutable: bool,
    window_start: datetime,
    window_end: datetime,
    account_key: str | None,
    depth: int,
    displaced: set,
    extended_window: bool,
    busy_snapshot: list[BusyEvent] | None = None,
    snapshot_range: tuple[datetime, datetime] | None = None,
) -> PlannedSlot:
    if depth >= MAX_REPAIR_DEPTH:
        raise ValueError("repair recursion limit reached")

    displaced.add(task.id)
    total = _block_total(duration, gaps.commute, out_s, in_s)
    with_trip = (out_s or in_s) and location is not None
    max_wait = (
        timedelta(minutes=get_settings().commute_home_layover_minutes)
        if with_trip
        else timedelta(0)
    )
    victims = _movable_victims(
        session,
        task,
        busy,
        duration,
        gaps,
        window_start,
        window_end,
        displaced=displaced,
    )
    if len(victims) > MAX_VICTIM_ATTEMPTS:
        log.info(
            "plan.schedule · trying %d of %d displacement candidates for task=%s",
            MAX_VICTIM_ATTEMPTS,
            len(victims),
            task.id,
        )
        victims = victims[:MAX_VICTIM_ATTEMPTS]
    for victim, victim_event, freed_range in victims:
        # Place the new task first, as if the victim had vacated (its legs
        # move with it), then reschedule the victim around that placement.
        # Blocking only the new slot — not the victim's old span — lets the
        # victim shift by minutes within its own former slot, e.g. sliding
        # 15 minutes to open a small hole. It also means a victim is only
        # ever moved once the task's spot is actually secured. The thinned
        # calendar gets the full placement repertoire again: a piggyback or
        # chain-insert can land a located task in a hole far smaller than
        # its standalone trip block.
        new_busy = [
            ev
            for ev in busy
            if ev.id != victim_event.id
            and not (ev.leg_key is not None and victim_event.id in ev.leg_key)
        ]
        planned: PlannedSlot | None = None
        if with_trip:
            planned = _piggyback_slot(
                new_busy,
                location=location,
                duration=duration,
                gaps=gaps,
                out_s=out_s,
                in_s=in_s,
                window_start=window_start,
                window_end=window_end,
                extended=extended_window,
                max_wait=max_wait,
            )
            if planned is None and not unroutable:
                planned = await _chain_insert_slot(
                    session,
                    new_busy,
                    location=location,
                    duration=duration,
                    gaps=gaps,
                    window_start=window_start,
                    window_end=window_end,
                    extended=extended_window,
                )
        if planned is None:
            slot = _slot_in_range(
                new_busy,
                total,
                gaps,
                freed_range,
                extended_window=extended_window,
            )
            if slot is not None:
                planned = _planned_from_block(slot, duration, gaps.commute, out_s, in_s)
        if planned is None:
            continue
        # Only an attempted victim enters the ledger — one skipped for lack
        # of a slot here may still open a placement in the extended pass.
        displaced.add(victim.id)
        # Pad by the event gap: the task isn't on the calendar yet, so the
        # victim's replan can only see it through this blocked range.
        block = Interval(
            planned.block_start - gaps.event,
            planned.block_end + gaps.event,
            victim_event.id,
        )
        try:
            # notify=False: a failed victim move keeps its old slot, so a
            # "could not schedule" for it would lie and get cleared moments
            # later by the next attempt.
            victim_planned = await _schedule_task_locked(
                session,
                victim,
                block=block,
                account_key=account_key,
                notify=False,
                _depth=depth + 1,
                _displaced=displaced,
                _keep_slot=True,
                _busy_snapshot=busy_snapshot,
                _snapshot_range=snapshot_range,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("plan.schedule · victim move failed task=%s err=%s", victim.id, exc)
            continue
        if victim_planned is None:
            # Victim couldn't be rescheduled — its old slot still holds.
            continue
        from app.services.notify import clear_task_notification

        await clear_task_notification(victim.id)
        return planned

    raise ValueError("no free slot before due date")


def _slot_in_range(
    busy: list[BusyEvent],
    duration: timedelta,
    gaps: Gaps,
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
            # Both extension phases straddle back into the normal window so a
            # forced block sits in the extension as little as possible — sweep
            # from the far edge (DAY_START up / DAY_TARGET down). See the
            # extended-mode bounds note and `_find_free_slot`.
            lower, upper = _day_bounds(
                day,
                DAY_START,
                END_OF_DAY,
                tz,
                freed_range.start,
                freed_range.end,
            )
            slot = _sweep_forward(busy, duration, gaps, lower, upper)
            if slot is not None:
                return slot
            lower, upper = _day_bounds(
                day,
                EARLY_MORNING,
                DAY_TARGET,
                tz,
                freed_range.start,
                freed_range.end,
            )
            slot = _sweep_backward(busy, duration, gaps, lower, upper)
            if slot is not None:
                return slot
        else:
            lower, upper = _day_bounds(
                day,
                DAY_START,
                DAY_TARGET,
                tz,
                freed_range.start,
                freed_range.end,
            )
            slot = _sweep_backward(busy, duration, gaps, lower, upper)
            if slot is not None:
                return slot
        day += timedelta(days=1)
    return None


def _movable_victims(
    session: Session,
    task: Task,
    busy: list[BusyEvent],
    duration: timedelta,
    gaps: Gaps,
    window_start: datetime,
    window_end: datetime,
    *,
    displaced: set = frozenset(),
) -> list[tuple[Task, BusyEvent, Interval]]:
    """Task events whose displacement would free a contiguous range large
    enough for the new task. The freed range counts adjacent gaps — a 30min
    victim sitting in a 1h hole between busy neighbours is still a candidate
    for a 1h task because moving it consolidates the surrounding free time.

    The prefilter only requires the bare task duration to fit — not the
    standalone trip block: a piggyback or chain-insert against the thinned
    calendar can shrink the legs to nothing, and the freed range may be
    bounded by window edges or the task's own vacated slot, which demand
    no gap at all. The placement retry enforces the real geometry.
    """
    min_span = duration
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
    if displaced:
        # A task already moved in this displacement chain won't be moved
        # again — mutual A↔B displacement otherwise ping-pongs to the depth
        # limit, patching the same events over and over.
        stmt = stmt.where(Task.id.not_in(list(displaced)))
    rows = list(session.execute(stmt).scalars())
    # Least urgent first: the victim with the latest due date has the most
    # room to be re-placed, so it should move before anything tighter.
    # Ties break toward location-less (moving a located task breaks its
    # trip chain and forces leg rework), then shortest (a 15-minute task
    # re-places far more easily than an hour-long one — and a failed victim
    # branch consumes its nested probes from the shared displacement
    # ledger, so the most promising candidate should go first).
    settings = get_settings()
    rows.sort(
        key=lambda row: (
            -row.due_date.timestamp(),
            1 if (row.location or "").strip() else 0,
            _duration_minutes(row, settings),
        )
    )
    return [
        (row, *by_event_id[row.calendar_event_id])
        for row in rows
        if row.calendar_event_id in by_event_id
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


def _gap_conflicts(
    events: list[BusyEvent],
    start: datetime,
    end: datetime,
    gaps: Gaps,
    *,
    inner_ids: set[str] = frozenset(),
    ignore_anchor_leg: tuple[str, int] | None = None,
    ignore_anchor_leg2: tuple[str, int] | None = None,
) -> list[BusyEvent]:
    out: list[BusyEvent] = []
    for ev in events:
        if ev.leg_key is not None:
            if ignore_anchor_leg and ev.leg_key[ignore_anchor_leg[1]] == ignore_anchor_leg[0]:
                continue
            if ignore_anchor_leg2 and ev.leg_key[ignore_anchor_leg2[1]] == ignore_anchor_leg2[0]:
                continue
        gap = gaps.neighbor_gap(ev, inner_ids)
        if ev.start < end + gap and start - gap < ev.end:
            out.append(ev)
    return out


def _latest_conflict(
    events: list[BusyEvent],
    start: datetime,
    end: datetime,
    gaps: Gaps,
) -> BusyEvent | None:
    conflicts = _gap_conflicts(events, start, end, gaps)
    if not conflicts:
        return None
    return max(conflicts, key=lambda ev: ev.start)


def _earliest_conflict(
    events: list[BusyEvent],
    start: datetime,
    end: datetime,
    gaps: Gaps,
) -> BusyEvent | None:
    conflicts = _gap_conflicts(events, start, end, gaps)
    if not conflicts:
        return None
    return min(conflicts, key=lambda ev: ev.end)


# --- Busy view -------------------------------------------------------------


def _db_scheduled_busy(
    session: Session,
    task: Task,
    window_start: datetime,
    window_end: datetime,
    busy: list[BusyEvent],
) -> list[BusyEvent]:
    """Reconcile the live calendar read against the DB, which is written
    under the scheduling lock right after every calendar patch and is
    therefore fresher than a lagging events.list.

    Tasks missing from the calendar view (just created, or deleted out from
    under us) are added, inflated by their cached leg durations so the
    backstop reserves the trip. Tasks *present* at a stale position — the
    events.list lag during a displacement cascade — get their span overridden
    with the DB truth; without this, two tasks can be written into the same
    slot. Added entries are marked immovable ("busy"): we won't displace a
    task we can only see in the DB."""
    from app.db.clients import tasks as tasks_store

    settings = get_settings()
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    out = list(busy)
    index = {ev.id: i for i, ev in enumerate(out)}
    for row in tasks_store.open_scheduled_between(
        session,
        time_min=window_start,
        time_max=window_end,
        exclude_task_id=task.id,
    ):
        start = to_user_tz(row.scheduled_date)
        end = start + timedelta(minutes=_duration_minutes(row, settings))
        i = index.get(row.calendar_event_id) if row.calendar_event_id else None
        if i is not None:
            if (out[i].start, out[i].end) != (start, end):
                log.info(
                    "plan.schedule · stale calendar span for task=%s event=%s; using DB slot %s",
                    row.id,
                    row.calendar_event_id,
                    start.isoformat(),
                )
                out[i] = replace(out[i], start=start, end=end)
            continue
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
    exclude_event_id: str | None = None,
    account_key: str | None = None,
) -> list[BusyEvent]:
    from app.services.calendar import (
        commute_leg_key,
        is_commute_event,
        is_free_event,
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
        if ev.id == exclude_event_id or is_free_event(ev):
            continue
        if ev.all_day:
            # Marked busy despite being all-day (those default to free): the
            # whole day is blocked, midnight to midnight in the user's tz.
            out.append(BusyEvent(ev.id, *_all_day_span(ev), "busy"))
            continue
        kind = "commute" if is_commute_event(ev) else "task" if is_task_event(ev) else "busy"
        location = await resolve_routable_location(ev.location)
        out.append(
            BusyEvent(
                ev.id,
                to_user_tz(ev.start),
                to_user_tz(ev.end),
                kind,
                location=location,
                leg_key=commute_leg_key(ev),
            )
        )
    return out


def _all_day_span(ev) -> tuple[datetime, datetime]:
    """All-day events carry bare dates (parsed as UTC midnights); the blocked
    range is those dates in the user's timezone, end date exclusive."""
    tz = user_tz()
    return (
        datetime.combine(ev.start.date(), time.min, tzinfo=tz),
        datetime.combine(ev.end.date(), time.min, tzinfo=tz),
    )


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
