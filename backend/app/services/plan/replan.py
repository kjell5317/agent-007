"""Global re-planning with the CP-SAT solver.

`replan()` reads every open task and the busy calendars for the horizon,
builds a `SolveInput`, solves it off the event loop, and applies
the difference: only tasks whose slot changed are patched on the calendar,
newly deferred ones lose their event, every placement is logged with its
feature snapshot. Commute legs are derived afterwards for the touched days
(phase 1 keeps commute out of the solver).

Triggers coalesce: a burst of requests runs at most one follow-up solve,
because a request is satisfied by any solve that started after it.

Task states for one solve:

* running (a "Still working" extension) or started — fixed;
* slot ended, check-in outcome still open — left alone;
* slot ended and missed, deferred, unplaced, or a future slot — movable.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import math
import time as _time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import labels as labels_store
from app.db.clients import schedule_log as log_store
from app.db.clients import tasks as tasks_store
from app.db.models.task import Task
from app.services.plan import features as F
from app.services.plan import routing
from app.services.plan import solver as S
from app.services.plan.checkin import awaiting_checkin, booked_slot, preempted, running_end
from app.services.plan.snapshot import CONTEXT_MARGIN, calendar_items, task_attrs, task_item
from app.services.plan.timeline import (
    Grid,
    Item,
    TaskAttrs,
    free_good_minutes,
    good_hours,
    meeting_minutes,
    lock_holds,
    lock_snapshot,
    slot_features,
)
from app.services.recurrence import shift
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

# Log-odds cost of moving an already placed task (fixed, not learned).
STABILITY_MOVE = 1.0
STABILITY_SOON = 3.0
# A hand-placed task whose lock was released (something new arrived, a
# deadline or duration changed) still only gives way when it's in the way:
# to avoid a missed deadline, a task with no slot, or a night-time slot —
# never for small preference gains.
STABILITY_YIELD = 5.0
SOON = timedelta(hours=24)
# Utility a deferral loses when the task alone would fill the free time
# expected before its deadline.
DEFER_RISK = 2.0
CAPACITY_WEEKS = 26
FREE_CACHE_SECONDS = 6 * 3600
# Due dates further out than this behave the same for the solver.
FAR_DUE_DAYS = 400


@dataclass
class ReplanResult:
    run_id: uuid.UUID
    ok: bool
    status: str = ""
    objective: float | None = None
    bound: float | None = None
    hint_objective: float | None = None
    wall_time: float = 0.0
    neighbour_mode: str = ""
    placed: dict[uuid.UUID, tuple[datetime, datetime]] = field(default_factory=dict)
    deferred: set[uuid.UUID] = field(default_factory=set)
    unplaced: set[uuid.UUID] = field(default_factory=set)
    moved: set[uuid.UUID] = field(default_factory=set)
    newly_unplaced: set[uuid.UUID] = field(default_factory=set)
    newly_late: set[uuid.UUID] = field(default_factory=set)
    routes: list | None = None


@dataclass
class _Movable:
    task: Task
    attrs: TaskAttrs
    minutes: int
    current_start: datetime | None
    solver_task: S.SolverTask


# Rain probabilities the last applied solve planned with, by local hour —
# the hourly weather refresh re-solves when they shift materially.
_last_rain: dict[str, float] = {}

# --- Request coalescing ------------------------------------------------------------

_requested = 0
_completed = 0
_last_result: ReplanResult | None = None
_pending_blocks: dict[uuid.UUID, list[tuple[datetime, datetime]]] = {}


async def request_replan(
    session: Session,
    *,
    reason: str,
    blocks: dict[uuid.UUID, list[tuple[datetime, datetime]]] | None = None,
    account_key: str | None = None,
    time_limit: float | None = None,
) -> ReplanResult:
    """Run (or join) a solve that reflects the state at the time of the call.

    `blocks` keep a task out of the given spans for this solve (a deleted
    event's slot, a "Reschedule" tap)."""
    from app.services.plan.schedule import _schedule_lock

    global _requested, _completed, _last_result
    _requested += 1
    ticket = _requested
    for task_id, spans in (blocks or {}).items():
        _pending_blocks.setdefault(task_id, []).extend(spans)
    async with _schedule_lock:
        if _completed >= ticket and _last_result is not None:
            return _last_result
        target = _requested
        run_blocks = dict(_pending_blocks)
        _pending_blocks.clear()
        result = await replan(
            session, reason=reason, blocks=run_blocks,
            account_key=account_key, time_limit=time_limit,
        )
        _completed = target
        _last_result = result
        return result


_batch: contextvars.ContextVar[list | None] = contextvars.ContextVar("replan_batch", default=None)


@contextlib.asynccontextmanager
async def batch(session: Session):
    """Collect solver-mode `schedule_task` calls and run one solve for all of
    them on exit — for loops that create several tasks at once (splits,
    repeat instances)."""
    token = _batch.set([])
    try:
        yield
        pending = _batch.get() or []
    finally:
        _batch.reset(token)
    if pending:
        from app.services.plan.schedule import schedule_tasks

        await schedule_tasks(session, pending)


def batched() -> list | None:
    return _batch.get()


# --- Shadow mode -----------------------------------------------------------------------

_shadow_task: asyncio.Task | None = None
_shadow_again = False


def shadow_soon(reason: str) -> None:
    """Dry-run the solver in the background after a legacy placement."""
    global _shadow_task, _shadow_again
    if _shadow_task is not None and not _shadow_task.done():
        _shadow_again = True
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _shadow_task = loop.create_task(_shadow_run(reason))


async def _shadow_run(reason: str, time_limit: float | None = None) -> None:
    global _shadow_again
    from app.db import SessionLocal

    while True:
        _shadow_again = False
        try:
            with SessionLocal() as session:
                await replan(session, reason=f"shadow:{reason}", dry_run=True, time_limit=time_limit)
        except Exception:  # noqa: BLE001 — shadow runs must never disturb the legacy planner
            log.exception("replan · shadow run failed")
        if not _shadow_again:
            return


# --- The solve --------------------------------------------------------------------------


async def replan(
    session: Session,
    *,
    reason: str,
    now: datetime | None = None,
    blocks: dict[uuid.UUID, list[tuple[datetime, datetime]]] | None = None,
    account_key: str | None = None,
    time_limit: float | None = None,
    dry_run: bool = False,
) -> ReplanResult:
    from app.services.learn import jobs

    settings = get_settings()
    tz = user_tz()
    now = (now or datetime.now(tz)).astimezone(tz)
    grid = Grid(now.date(), settings.scheduler_horizon_days, tz)
    horizon_start, horizon_end = grid.at(0), grid.at(grid.horizon)
    run_id = uuid.uuid4()

    label_defaults = labels_store.defaults_by_name(session)
    calendar = await calendar_items(
        session, horizon_start - CONTEXT_MARGIN, horizon_end + CONTEXT_MARGIN,
        account_key=account_key, blockers=True,
    )
    # Location blockers are route nodes only, never fixed items: located
    # tasks route around them, location-less ones may sit inside.
    events = [item for item in calendar if item.kind == "event"]
    blockers = [item for item in calendar if item.kind == "blocker"]
    weights = jobs.weights_for(session, grid.origin)

    tasks = tasks_store.open_schedulable(session)
    resolved = log_store.by_slot_for_tasks(
        session, [t.id for t in tasks], log_store.RESOLUTION_KINDS | {"closed"}
    )
    lead = timedelta(minutes=settings.slot_min_lead_minutes)
    earliest_now = grid.ceil(now + lead)

    # New rows carry a provisional `scheduled_date` (creation time) until
    # a calendar event exists; only a mirrored slot is a real one.
    slots = {
        t.id: booked_slot(t) if t.schedule_state == "placed" and t.calendar_event_id else None
        for t in tasks
    }
    overtime = [
        (slot[1], running_end(slot[0], slot[1], now))
        for t in tasks
        if t.running_since is not None and (slot := slots[t.id]) is not None
    ]

    fixed_items: list[Item] = []
    fixed_tasks: dict[uuid.UUID, Item] = {}
    candidates: list[tuple[Task, TaskAttrs, datetime | None]] = []
    for task in tasks:
        attrs = task_attrs(task, label_defaults)
        slot = slots[task.id]
        if task.running_since is not None and slot is not None:
            item = task_item(task, attrs, slot[0], max(slot[1], running_end(slot[0], slot[1], now)))
            fixed_tasks[task.id] = item
        elif slot is not None and slot[0] <= now and preempted(slot, overtime):
            candidates.append((task, attrs, None))
        elif slot is not None and slot[0] <= now < slot[1]:
            fixed_tasks[task.id] = task_item(task, attrs, *slot)
        elif slot is not None and slot[1] <= now:
            if awaiting_checkin(slot[1], now, bool(resolved.get((task.id, task.scheduled_date)))):
                continue
            candidates.append((task, attrs, None))
        else:
            candidates.append((task, attrs, slot[0] if slot else None))
    locked, yielding = _hand_placed_locks(
        session, candidates, tasks, events, list(fixed_tasks.values()), now=now, dry_run=dry_run,
    )
    for task, attrs, current in candidates:
        if task.id in locked:
            fixed_tasks[task.id] = task_item(task, attrs, *booked_slot(task))
    candidates = [c for c in candidates if c[0].id not in locked]
    fixed_items = events + list(fixed_tasks.values())

    movable: dict[uuid.UUID, _Movable] = {}
    for task, attrs, current in candidates:
        movable[task.id] = _movable(
            session, task, attrs, current,
            grid=grid, now=now, earliest_now=earliest_now,
            stability_floor=STABILITY_YIELD if task.id in yielding else 0.0,
            blocks=(blocks or {}).get(task.id, []),
        )
    open_ids = {t.id for t in tasks}
    for task_id in locked:
        task = next(t for t in tasks if t.id == task_id)
        before = movable.get(task.depends_on_task_id)
        if before is not None:
            # A hand-placed successor stays put; its predecessor must end first.
            start = grid.floor(fixed_tasks[task_id].start)
            before.solver_task.forbidden += ((start, grid.horizon),)
    for m in movable.values():
        pred = m.task.depends_on_task_id
        if pred in movable:
            m.solver_task.predecessor = str(pred)
        elif pred in fixed_tasks:
            m.solver_task.earliest = max(m.solver_task.earliest, grid.ceil(fixed_tasks[pred].end))
        elif pred is not None and pred in open_ids:
            # Predecessor waits for its check-in answer: keep the order.
            pred_task = next(t for t in tasks if t.id == pred)
            pred_slot = booked_slot(pred_task)
            if pred_slot is not None:
                m.solver_task.earliest = max(m.solver_task.earliest, grid.ceil(pred_slot[1]))

    rho = jobs.rho(session)
    capacity: list[S.CapacityBound] = []
    deferrable = [m for m in movable.values() if m.solver_task.deferrable]
    if deferrable:
        free_by_day = await _future_free_minutes(session, grid, weights)
        capacity = _capacity_bounds(grid, free_by_day, rho, [m.solver_task.due for m in deferrable])
        for m in deferrable:
            m.solver_task.defer_utility = _defer_utility(
                m.attrs, m.minutes, grid=grid, weights=weights,
                free_until_due=_free_until(grid, free_by_day, m.attrs.due), rho=rho,
            )

    _warm_start(grid, list(movable.values()), fixed_items, weights)

    routing_ctx = None
    if routing.routing_enabled():
        try:
            routing_ctx = await routing.build(
                session, grid=grid, now=now, events=[*events, *blockers], fixed_tasks=fixed_tasks,
                movable=[(m.solver_task.key, m.task, m.attrs) for m in movable.values()],
                weights=weights,
            )
        except Exception as exc:  # noqa: BLE001 — solve without commute rather than not at all
            log.warning("replan · routing input failed, solving without commute err=%s", exc)

    inp = S.SolveInput(
        days=grid.dates,
        tasks=[m.solver_task for m in movable.values()],
        fixed=[_fixed(grid, item) for item in fixed_items],
        fixed_load=[
            sum(_load_on(item, day, tz) for item in fixed_items) for day in grid.dates
        ],
        weights=weights,
        capacity=capacity,
        meeting_load=[meeting_minutes(fixed_items, day, tz) for day in grid.dates],
        commute=routing_ctx.commute if routing_ctx is not None else None,
        time_limit=time_limit or settings.scheduler_time_limit_seconds,
        workers=settings.scheduler_workers,
        neighbour_mode=(
            "candidate" if len(movable) > settings.scheduler_neighbour_task_limit else "all"
        ),
    )
    solved = await asyncio.to_thread(S.solve, inp)
    result = ReplanResult(
        run_id=run_id, ok=solved.ok, status=solved.status, objective=solved.objective,
        bound=solved.bound, hint_objective=solved.hint_objective, wall_time=solved.wall_time,
        neighbour_mode=solved.neighbour_mode,
    )
    for m in movable.values():
        start = solved.starts.get(m.solver_task.key)
        if start is None:
            (result.deferred if m.solver_task.deferrable else result.unplaced).add(m.task.id)
            continue
        begin = grid.at(start)
        result.placed[m.task.id] = (begin, begin + timedelta(minutes=m.minutes))
        if m.solver_task.current != start:
            result.moved.add(m.task.id)
    result.routes = solved.routes

    _trace(reason, result, movable, grid, dry_run=dry_run)
    if dry_run or not solved.ok:
        if not solved.ok:
            log.error("replan · no solution status=%s; keeping the current schedule", solved.status)
        return result
    await _apply(
        session, result, movable, grid=grid, now=now, fixed_items=fixed_items,
        routing_ctx=routing_ctx,
    )
    if routing_ctx is not None:
        global _last_rain
        _last_rain = routing.rain_snapshot(routing_ctx, grid, now)
    return result


def _hand_placed_locks(
    session: Session,
    candidates: list,
    tasks: list[Task],
    events: list[Item],
    fixed: list[Item],
    *,
    now: datetime,
    dry_run: bool,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """`(locked, yielding)` hand-placed tasks. Locked ones stay exactly where
    they are: nothing new was issued and no deadline or duration changed
    since the placement (see `lock_holds`). The rest of the hand-placed
    tasks — released by such a change, or overlapped by a fixed item —
    yield only when they're in the way (`STABILITY_YIELD`)."""
    from app.services.plan.schedule import estimate_minutes

    settings = get_settings()
    current_tasks = [(str(t.id), to_user_tz(t.due_date), estimate_minutes(t, settings)) for t in tasks]
    tasks_created = [(str(t.id), to_user_tz(t.created_at) if t.created_at else now) for t in tasks]
    events_created = [(e.event_id, e.created) for e in events if e.kind == "event" and e.event_id]
    taken = [(item.start, item.end) for item in [*events, *fixed]]
    out: set[uuid.UUID] = set()
    yielding: set[uuid.UUID] = set()
    for task, _, current in candidates:
        if task.placed_by != "user" or current is None or current <= now:
            continue
        slot = booked_slot(task)
        placed_at = to_user_tz(task.placed_at) if task.placed_at else now
        snapshot = lock_snapshot(slot, placed_at, current_tasks, events)
        stored = task.placed_context if (task.placed_context or {}).get("tasks") is not None else None
        if stored is None:
            # Taken at the first solve after the move: leave out what was
            # issued in between, so it still counts as new.
            created = dict(tasks_created)
            stored = lock_snapshot(
                slot,
                placed_at,
                [t for t in current_tasks if created[t[0]] <= placed_at],
                [e for e in events if e.created is None or e.created <= placed_at],
            )
            if not dry_run:
                task.placed_context = stored
                # Commit, not flush: the solve awaits for seconds, and a row
                # lock held across that freezes the loop on any sync write to it.
                session.commit()
        holds = lock_holds(stored, snapshot, tasks_created=tasks_created, events_created=events_created)
        if not holds or any(start < slot[1] and slot[0] < end for start, end in taken):
            yielding.add(task.id)
            continue
        out.add(task.id)
        taken.append(slot)
    return out, yielding


def _movable(
    session: Session,
    task: Task,
    attrs: TaskAttrs,
    current: datetime | None,
    *,
    grid: Grid,
    now: datetime,
    earliest_now: int,
    stability_floor: float,
    blocks: list[tuple[datetime, datetime]],
) -> _Movable:
    from app.services.plan.schedule import estimate_minutes

    settings = get_settings()
    # Booked exactly as set: the learned correction shapes a task's length
    # once, at creation (`with_learned_estimate`), never at planning time.
    estimate = estimate_minutes(task, settings)
    minutes = max(S.TASK_MINUTES, math.ceil(estimate / S.TASK_MINUTES) * S.TASK_MINUTES)

    earliest = earliest_now
    occurrence = _repeat_occurrence(session, task)
    if occurrence is not None:
        period_start = shift(
            to_user_tz(occurrence.due_date), occurrence.recurrence_unit, -occurrence.recurrence_interval,
        )
        earliest = max(earliest, grid.ceil(period_start))
    due = min(grid.floor(attrs.due), grid.horizon + FAR_DUE_DAYS * S.SLOTS_PER_DAY)

    current_slot = grid.floor(current) if current is not None else None
    stability = 0.0
    if current is not None:
        stability = max(stability_floor, STABILITY_SOON if current - now < SOON else STABILITY_MOVE)

    solver_task = S.SolverTask(
        key=str(task.id),
        dur=minutes // S.SLOT_MINUTES,
        earliest=earliest,
        due=due,
        deadline_kind=attrs.deadline_kind,
        label=attrs.label,
        work_type=attrs.work_type,
        effort=attrs.effort,
        current=current_slot,
        stability=stability,
        neutral=stability_floor > 0,
        deferrable=due >= grid.horizon,
        forbidden=tuple((grid.floor(s), grid.ceil(e)) for s, e in blocks),
    )
    return _Movable(task, attrs, minutes, current, solver_task)


def _repeat_occurrence(session: Session, task: Task) -> Task | None:
    if task.recurrence_unit:
        return task
    if task.parent_task_id:
        parent = tasks_store.get(session, task.parent_task_id)
        if parent is not None and parent.recurrence_unit:
            return parent
    return None


def _fixed(grid: Grid, item: Item) -> S.FixedItem:
    return S.FixedItem(
        start=grid.floor(item.start), end=grid.ceil(item.end), kind=item.kind,
        label=item.label, work_type=item.work_type, effort=item.effort,
    )


def _load_on(item: Item, day: date, tz) -> float:
    from app.services.plan.timeline import item_load_on

    return item_load_on(item, day, tz)


# --- Deferral ----------------------------------------------------------------------------

_free_cache: dict[tuple, tuple[float, dict[date, float]]] = {}


async def _future_free_minutes(session: Session, grid: Grid, weights: dict) -> dict[date, float]:
    """Free good-hour minutes per day after the horizon, for `CAPACITY_WEEKS`.
    Cached: coarse by design, and a months-long calendar read per solve
    would be wasteful."""
    hours = good_hours(weights)
    key = (grid.origin, grid.days, tuple(sorted(hours)))
    cached = _free_cache.get(key)
    if cached is not None and _time.monotonic() - cached[0] < FREE_CACHE_SECONDS:
        return cached[1]
    first = grid.origin + timedelta(days=grid.days)
    last = first + timedelta(weeks=CAPACITY_WEEKS)
    try:
        items = await calendar_items(session, grid.at(grid.horizon), grid.at(grid.horizon) + timedelta(weeks=CAPACITY_WEEKS))
    except Exception as exc:  # noqa: BLE001 — fall back to an empty calendar
        log.warning("replan · future busy read failed err=%s", exc)
        items = []
    free = free_good_minutes(items, first, last, hours, grid.tz)
    _free_cache.clear()
    _free_cache[key] = (_time.monotonic(), free)
    return free


def _free_until(grid: Grid, free_by_day: dict[date, float], due: datetime) -> float:
    last = due.astimezone(grid.tz).date()
    return sum(minutes for day, minutes in free_by_day.items() if day < last)


def _capacity_bounds(
    grid: Grid, free_by_day: dict[date, float], rho: float, dues: list[int]
) -> list[S.CapacityBound]:
    latest = max(dues)
    first = grid.origin + timedelta(days=grid.days)
    out: list[S.CapacityBound] = []
    for week in range(1, CAPACITY_WEEKS + 1):
        boundary = first + timedelta(weeks=week)
        slot = (boundary - grid.origin).days * S.SLOTS_PER_DAY
        free = sum(minutes for day, minutes in free_by_day.items() if day < boundary)
        out.append(S.CapacityBound(due_by=slot, capacity_min=rho * free))
        if slot >= latest:
            break
    return out


def _defer_utility(
    attrs: TaskAttrs,
    minutes: int,
    *,
    grid: Grid,
    weights: dict[str, float],
    free_until_due: float,
    rho: float,
) -> float:
    """The task's utility on a representative good slot just past the
    horizon, minus a risk that grows as it would fill the free time left
    before its deadline."""
    day = grid.origin + timedelta(days=grid.days)
    hours = []
    for hour in range(24):
        end_hour = (hour * 60 + minutes - 1) // 60 % 24
        hours.append((
            F.utility(F.start_hour_features(hour, attrs.work_type, attrs.effort), weights)
            + F.utility(F.end_hour_features(end_hour), weights),
            hour,
        ))
    best = sorted(hours, reverse=True)[:3]
    hour_utility = sum(u for u, _ in best) / len(best)
    start = datetime.combine(day, datetime.min.time(), tzinfo=grid.tz) + timedelta(hours=best[0][1])
    utility = hour_utility
    utility += F.utility(F.day_features(day, grid.origin, attrs.work_type), weights)
    utility += F.utility(
        F.deadline_features(start + timedelta(minutes=minutes), attrs.due, attrs.deadline_kind), weights
    )
    for side in F.SIDES:
        utility += F.utility(F.neighbour_features(
            side, None, label=attrs.label, work_type=attrs.work_type, effort=attrs.effort
        ), weights)
        utility += F.utility(F.gap_features(side, None), weights)
    utility += F.utility(F.load_features(0), weights)
    risk = DEFER_RISK * minutes / max(rho * free_until_due, minutes)
    return utility - risk


# --- Warm start --------------------------------------------------------------------------


def _warm_start(
    grid: Grid,
    movable: list[_Movable],
    fixed_items: list[Item],
    weights: dict[str, float],
) -> None:
    """Hint = the current schedule where it's still valid; tasks without a
    valid slot are placed greedily on their cheapest free start."""
    occupied: list[tuple[int, int]] = [
        (grid.floor(i.start), grid.ceil(i.end)) for i in fixed_items
    ]

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in occupied)

    def allowed(t: S.SolverTask, start: int) -> bool:
        if start < 0 or start + t.dur > grid.horizon:
            return False
        if start != t.current and (start < t.earliest or start % S.TASK_GRID):
            return False
        return all(start + t.dur <= fs or start >= fe for fs, fe in t.forbidden)

    by_key = {m.solver_task.key: m.solver_task for m in movable}
    ordered = sorted(
        (m.solver_task for m in movable if m.solver_task.current is not None),
        key=lambda t: t.current,
    )
    for t in ordered:
        if allowed(t, t.current) and free(t.current, t.current + t.dur):
            t.hint = t.current
            occupied.append((t.current, t.current + t.dur))

    def order_ok(t: S.SolverTask) -> bool:
        pred = by_key.get(t.predecessor) if t.predecessor else None
        return pred is None or (pred.hint is not None and t.hint >= pred.hint + pred.dur)

    for t in ordered:
        if t.hint is not None and not order_ok(t):
            occupied.remove((t.hint, t.hint + t.dur))
            t.hint = None

    for m in sorted(movable, key=lambda m: m.solver_task.due):
        t = m.solver_task
        if t.hint is not None:
            continue
        if t.deferrable and m.task.schedule_state == "deferred":
            continue
        earliest = t.earliest
        if t.predecessor:
            pred = by_key.get(t.predecessor)
            if pred is None or pred.hint is None:
                continue
            earliest = max(earliest, pred.hint + pred.dur)
        best = _greedy_start(t, earliest, occupied, grid, weights, allowed)
        if best is not None:
            t.hint = best
            occupied.append((best, best + t.dur))


def _greedy_start(t, earliest, occupied, grid, weights, allowed) -> int | None:
    start_table = [F.utility(F.start_hour_features(h, t.work_type, t.effort), weights) for h in range(24)]
    end_table = [F.utility(F.end_hour_features(h), weights) for h in range(24)]
    days = grid.dates
    day_table = [F.utility(F.day_features(d, days[0], t.work_type), weights) for d in days]
    flag = weights.get(F.late_flag_key(t.deadline_kind), 0.0)
    per_hour = weights.get(F.late_hours_key(t.deadline_kind), 0.0)
    gaps = sorted(occupied)
    best, best_utility = None, -math.inf
    cursor = max(earliest, 0)
    bounds = [(s, e) for s, e in gaps if e > cursor] + [(grid.horizon, grid.horizon)]
    for s, e in bounds:
        first = -(-cursor // S.TASK_GRID) * S.TASK_GRID
        for start in range(first, s - t.dur + 1, S.TASK_GRID):
            if not allowed(t, start):
                continue
            end = start + t.dur
            u = (
                start_table[(start % S.SLOTS_PER_DAY) // S.SLOTS_PER_HOUR]
                + end_table[((end - 1) % S.SLOTS_PER_DAY) // S.SLOTS_PER_HOUR]
                + day_table[start // S.SLOTS_PER_DAY]
            )
            if end > t.due:
                u += flag + per_hour * (end - t.due) / S.SLOTS_PER_HOUR
            if u > best_utility:
                best, best_utility = start, u
        cursor = max(cursor, e)
    return best


# --- Apply ------------------------------------------------------------------------------


async def _apply(
    session: Session,
    result: ReplanResult,
    movable: dict[uuid.UUID, _Movable],
    *,
    grid: Grid,
    now: datetime,
    fixed_items: list[Item],
    routing_ctx: routing.RoutingContext | None = None,
) -> None:
    from app.events import publish_task
    from app.services.calendar import delete_task_event, update_task_event
    from app.services.plan.schedule import estimate_minutes

    settings = get_settings()
    final_items = list(fixed_items) + [
        task_item(movable[tid].task, movable[tid].attrs, *span) for tid, span in result.placed.items()
    ]
    touched: list[tuple[datetime, datetime]] = []
    changed: list[uuid.UUID] = []
    # A task split into a container or closed while the solve ran keeps no slot.
    schedulable = {t.id for t in tasks_store.open_schedulable(session)}
    for task_id, m in movable.items():
        if task_id not in schedulable:
            continue
        task = m.task
        prior = booked_slot(task) if task.schedule_state == "placed" else None
        span = result.placed.get(task_id)
        if span is not None:
            booked = m.minutes if m.minutes != estimate_minutes(task, settings) else None
            unchanged = (
                task.schedule_state == "placed"
                and m.current_start is not None
                and m.solver_task.current == grid.floor(span[0])
                and task.calendar_event_id
                and task.booked_minutes == booked
            )
            if unchanged:
                continue
            try:
                task.booked_minutes = booked
                await update_task_event(session, task, start=span[0], end=span[1])
            except Exception as exc:  # noqa: BLE001
                log.error("replan · calendar write failed task=%s err=%s", task_id, exc, exc_info=True)
                session.rollback()
                continue
            task.schedule_state = "placed"
            task.placed_by = "planner"
            task.placed_at = now
            task.placed_context = None
            feats = {
                "slot": slot_features(
                    m.attrs, span[0], span[1], now=now, items=final_items, tz=grid.tz,
                ),
                "meta": {"estimate": task.agent_estimation or estimate_minutes(task, settings),
                         "label": m.attrs.label,
                         "work_type": m.attrs.work_type, "effort": m.attrs.effort},
            }
            log_store.add(
                session, task_id=task_id, kind="placed", actor="planner",
                slot_start=span[0], slot_end=span[1],
                prev_start=prior[0] if prior else None, prev_end=prior[1] if prior else None,
                solver_run_id=result.run_id, features=feats,
            )
            session.commit()
            touched.append(span)
            if prior:
                touched.append(prior)
            changed.append(task_id)
            due = m.attrs.due
            if span[1] > due > now and (prior is None or prior[1] <= due):
                result.newly_late.add(task_id)
            continue

        state = "deferred" if m.solver_task.deferrable else "unplaced"
        if task.schedule_state == state and task.scheduled_date is None:
            continue
        if task.calendar_event_id:
            await delete_task_event(session, task)
        task.scheduled_date = None
        task.booked_minutes = None
        task.schedule_state = state
        task.placed_by = "planner"
        task.placed_context = None
        log_store.add(
            session, task_id=task_id, kind=state, actor="planner",
            prev_start=prior[0] if prior else None, prev_end=prior[1] if prior else None,
            solver_run_id=result.run_id,
        )
        session.commit()
        if prior:
            touched.append(prior)
        changed.append(task_id)
        if state == "unplaced":
            result.newly_unplaced.add(task_id)

    session.commit()
    for task_id in changed:
        publish_task(session, task_id)
    # Deadline warnings go out whoever triggered the solve — a repeat
    # instance or a missed slot re-plan included.
    from app.services.notify import notify_deadline_missed, notify_no_slot

    for task_id in result.newly_unplaced:
        await notify_no_slot(movable[task_id].task)
    for task_id in result.newly_late:
        start, end = result.placed[task_id]
        await notify_deadline_missed(movable[task_id].task, start=start, end=end)
    if routing_ctx is not None and result.routes is not None:
        await _write_routes(session, routing_ctx, result, movable, grid=grid, now=now,
                            final_items=final_items)
    else:
        await _replan_commutes(session, touched)


async def _write_routes(session, ctx, result, movable, *, grid, now, final_items) -> None:
    """Write the solved commute legs. New tasks only got their calendar event
    in this apply, so their provisional `task:<id>` keys are mapped first."""
    anchors = {f"task:{m.task.id}": m.task.calendar_event_id for m in movable.values()}
    legs = routing.planned_legs(ctx, result.routes, grid=grid, anchors=anchors)
    obstacles = []
    for item in final_items:
        if item.kind == "event":
            obstacles.append((item.event_id or item.ref, item.label or "event", item.start, item.end))
        else:
            task = movable[item.task_id].task if item.task_id in movable else None
            anchor = task.calendar_event_id if task is not None else item.event_id
            obstacles.append((anchor or item.ref, "task", item.start, item.end))
    try:
        summary = await routing.write_legs(
            session, ctx, legs, now=now, horizon_end=grid.at(grid.horizon), obstacles=obstacles,
        )
        log.info("replan · commute legs %s", summary)
    except Exception as exc:  # noqa: BLE001 — the placements stand; the next solve rewrites legs
        log.error("replan · writing commute legs failed err=%s", exc, exc_info=True)


async def _replan_commutes(session: Session, spans: list[tuple[datetime, datetime]]) -> None:
    if not spans or not get_settings().commute_enabled:
        return
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    margin = commute_window_margin()
    windows: list[list[datetime]] = []
    for start, end in sorted(spans):
        lo, hi = start - margin, end + margin
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])
    for lo, hi in windows:
        await plan_commutes_window_best_effort(session, window_start=lo, window_end=hi, _depth=1)


def _trace(reason: str, result: ReplanResult, movable: dict, grid: Grid, *, dry_run: bool) -> None:
    from app import observability

    engine = get_settings().scheduler_engine
    placements = []
    for task_id, m in movable.items():
        span = result.placed.get(task_id)
        placements.append({
            "task_id": str(task_id),
            "title": (m.task.title or "")[:80],
            "current": m.current_start.isoformat() if m.current_start else None,
            "current_state": m.task.schedule_state,
            "solver": span[0].isoformat() if span else (
                "deferred" if task_id in result.deferred else "unplaced"
            ),
        })
    summary = {
        "status": result.status,
        "objective": result.objective,
        "bound": result.bound,
        "current_objective": result.hint_objective,
        "wall_time": round(result.wall_time, 2),
        "neighbour_mode": result.neighbour_mode,
        "movable": len(movable),
        "moved": len(result.moved),
        "deferred": len(result.deferred),
        "unplaced": len(result.unplaced),
    }
    if result.routes is not None:
        summary["legs"] = len(result.routes)
        summary["legs_by_bike"] = sum(leg.mode == "bicycling" for leg in result.routes)
        summary["legs_shortened"] = sum(leg.shortfall > 0 for leg in result.routes)
    log.info("replan · %s dry_run=%s %s", reason, dry_run, summary)
    with observability.root_span("scheduler.solve", tags=["scheduler", engine]) as span:
        span.update(
            input={"reason": reason, "dry_run": dry_run, "horizon_days": grid.days},
            output={**summary, "placements": placements},
            metadata={"run_id": str(result.run_id)},
        )
