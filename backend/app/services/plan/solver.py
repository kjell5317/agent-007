"""CP-SAT model of the task schedule — pure, no I/O.

Time is a grid of 5-minute slots, `t = 0` at local midnight of the first
horizon day. Tasks start on the 15-minute grid (`TASK_GRID` slots) and last
whole quarters, back to back where they fit; only commute legs use the
5-minute resolution. Hard constraints are only what is physically or logically
required: no overlap between tasks and fixed items (calendar events, started
or running tasks), earliest starts (now, a repeat occurrence's period, a fixed
predecessor), predecessor order between movable tasks, and per-task forbidden
spans. Everything else — day windows, gaps, neighbours, lateness, day load —
is a cost built from the preference weights through the same helpers that
produce the learner's features (`app.services.plan.features`).

Costs are integers: log-odds × `SCALE`. Tables stay small (24 hours, one
entry per day) because a per-start table makes presolve explode.

Gap and neighbour terms use literals per (task, item it could sit next to):
"ends 0 / 5 / 10 min before", "starts 0 / 5 / 10 min after", and "within
60 min". Each side of a task picks at most one neighbour among the items
within 60 min, and must pick the item at a 0–10 minute gap when there is one.
Above `NEIGHBOUR_TASK_LIMIT` movable tasks those literals are only built on
each task's candidate days (its current day plus its three best), and a task
landing elsewhere is scored as having no neighbours.

Commute (optional `SolveInput.commute`): each day is one ordered path —
`AddCircuit` from a start node (where the user is) through located items and
up to `home_stops` optional home stops to an end node at home. Every node's
arrival leg is a variable-size interval in the global no-overlap; its mode is
bike or transit. The bike's location is an integer state carried along the
path and across nights: a leg can be ridden only from where the bike is, and
the bike may stay anywhere. Ride length, rain and the rest are costs from
the preference weights; buffers sit inside the leg intervals.

The warm start (`SolverTask.hint`) is completed into a full assignment before
the solve — CP-SAT ignores an incomplete hint — so the result is never worse
than the hinted schedule. With commute, a route simulation fixes the warm
start's discrete routing choices first.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from ortools.sat.python import cp_model

from app.services.plan import features as F

log = logging.getLogger(__name__)

SLOT_MINUTES = 5
SLOTS_PER_HOUR = 12
SLOTS_PER_DAY = 288
TASK_GRID = 3
TASK_MINUTES = TASK_GRID * SLOT_MINUTES
NEIGHBOUR_SLOTS = F.NEIGHBOUR_MINUTES // SLOT_MINUTES
SCALE = 1000
UNPLACED_COST = 10**8
# Per minute of deferred work beyond the expected future capacity.
CAPACITY_SLACK_COST = 2 * SCALE
NEIGHBOUR_TASK_LIMIT = 50
CANDIDATE_DAYS = 3

HOME = "home"
# Fixed commute costs (log-odds × SCALE), not learned.
SHORTFALL_COST = round(0.5 * SLOT_MINUTES * SCALE)  # per slot a leg is shorter than its travel
AWAY_COST = round(0.01 * SLOT_MINUTES * SCALE)  # per slot away from home
EXTRA_TRIP_COST = round(0.3 * SCALE)
BIKE_AWAY_NIGHT_COST = round(0.3 * SCALE)
UNROUTABLE_COST = 3 * SCALE
MODE_CHANGE_COST = 2 * SCALE
PIN_COST = 20 * SCALE
UNROUTABLE_SLOTS = 30 // SLOT_MINUTES
MODE_CHANGE_WINDOW = 2 * SLOTS_PER_HOUR


@dataclass(frozen=True)
class FixedItem:
    start: int
    end: int
    kind: str  # "event" | "task"
    label: str | None = None
    work_type: str | None = None
    effort: str | None = None


@dataclass
class SolverTask:
    key: str
    dur: int
    earliest: int
    due: int
    deadline_kind: str = "firm"
    label: str | None = None
    work_type: str | None = None
    effort: str | None = None
    current: int | None = None
    # Log-odds cost of leaving `current`.
    stability: float = 0.0
    deferrable: bool = False
    # Utility of deciding later instead of placing now (deferrable only).
    defer_utility: float = 0.0
    predecessor: str | None = None
    forbidden: tuple[tuple[int, int], ...] = ()
    # Warm start; None = not placed in the warm start.
    hint: int | None = None
    # No slot preferences of its own (a released hand-placed task): it moves
    # only for its deadline or for what other tasks need.
    neutral: bool = False


@dataclass(frozen=True)
class RouteNode:
    """A fixed located item (or cluster of overlapping ones): arriving legs
    target `key_in`, departing legs leave from `key_out`."""

    key_in: str
    key_out: str
    start: int
    end: int
    loc: int
    physical: bool = False


@dataclass
class CommuteInput:
    # Ordered location pair → (bike slots | None, transit slots | None),
    # travel only (buffers are added by the model). Location 0 is home.
    travel: dict[tuple[int, int], tuple[int | None, int | None]]
    # (from, to, mode) → utility of the leg itself (minutes, ride bucket, …).
    leg_utility: dict[tuple[int, int, str], float]
    # Per horizon hour: utility of the weather when riding then.
    ride_hour_utility: list[float]
    after_physical_utility: float = 0.0
    fixed: list[RouteNode] = field(default_factory=list)
    # Movable located task key → location, its anchor key (calendar event id
    # once it has one) and whether it's physical / high effort.
    task_locations: dict[str, int] = field(default_factory=dict)
    task_anchors: dict[str, str] = field(default_factory=dict)
    task_physical: dict[str, bool] = field(default_factory=dict)
    start_location: int = 0
    start_key: str = HOME
    available_from: int = 0
    bike_start: int = 0
    buffer: int = 1
    # Upper bound on trips home between away items per day.
    home_stops: int = 4
    candidate_days: int = CANDIDATE_DAYS
    # (origin anchor, dest anchor) → mode the user flipped the leg to.
    pins: dict[tuple[str, str], str] = field(default_factory=dict)
    # (origin anchor, dest anchor) → (mode, depart slot) of the current plan.
    current: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    now: int = 0


@dataclass
class RouteLeg:
    day: int
    origin_key: str
    dest_key: str
    origin_loc: int
    dest_loc: int
    mode: str  # bicycling | transit | failed
    start: int  # leg interval, buffers included
    end: int
    travel: int
    shortfall: int
    bike_before: int
    bike_after: int
    pinned: bool = False


@dataclass(frozen=True)
class CapacityBound:
    """Deferred work due by `due_by` must fit `capacity_min` minutes."""

    due_by: int
    capacity_min: float


@dataclass
class SolveInput:
    days: list[date]
    tasks: list[SolverTask]
    fixed: list[FixedItem] = field(default_factory=list)
    fixed_load: list[float] = field(default_factory=list)
    weights: dict[str, float] = field(default_factory=F.prior_mean)
    capacity: list[CapacityBound] = field(default_factory=list)
    # Minutes in meetings per horizon day.
    meeting_load: list[float] = field(default_factory=list)
    commute: CommuteInput | None = None
    time_limit: float = 10.0
    workers: int = 2
    seed: int = 7
    neighbour_mode: str = "auto"  # "auto" | "all" | "candidate"

    @property
    def horizon(self) -> int:
        return len(self.days) * SLOTS_PER_DAY


@dataclass
class SolveResult:
    starts: dict[str, int | None]
    status: str
    objective: float | None
    bound: float | None
    hint_objective: float | None
    wall_time: float
    neighbour_mode: str
    # True when the solve found nothing better than the warm start, or
    # nothing at all (`ok` False): the caller keeps the current schedule.
    from_hint: bool = False
    ok: bool = True
    routes: list[RouteLeg] | None = None


def _cost(utility: float) -> int:
    return round(-utility * SCALE)


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


@dataclass
class _TaskVars:
    task: SolverTask
    spans: list[list[int]]
    start: cp_model.IntVar
    present: cp_model.IntVar
    interval: cp_model.IntervalVar
    day_lits: dict[int, cp_model.IntVar] = field(default_factory=dict)
    day_table: dict[int, int] = field(default_factory=dict)
    hour_tables: tuple[list[int], list[int]] = ([], [])
    candidate_days: set[int] | None = None
    route_days: set[int] = field(default_factory=set)
    # side -> list of (neighbour literal, within-60 literal)
    side_lits: dict[str, list[tuple[cp_model.IntVar, cp_model.IntVar]]] = field(
        default_factory=lambda: {"prev": [], "next": []}
    )

    @property
    def lo(self) -> int:
        return self.spans[0][0]

    @property
    def hi(self) -> int:
        return self.spans[-1][1]

    def allows(self, start: int) -> bool:
        on_grid = start % TASK_GRID == 0 or start == self.task.current
        return on_grid and any(lo <= start <= hi for lo, hi in self.spans)


class _Builder:
    def __init__(self, inp: SolveInput):
        self.inp = inp
        self.m = cp_model.CpModel()
        self.w = inp.weights
        self.H = inp.horizon
        self.vars: dict[str, _TaskVars] = {}
        self.terms: list = []
        self.constant = 0
        self.intervals: list = []
        self.routes: dict[int, dict] = {}

    # -- helpers --------------------------------------------------------------

    def _bool(self, name: str) -> cp_model.IntVar:
        return self.m.new_bool_var(name)

    def _reify_range(self, lit, expr, lo: int, hi: int, enforce: list) -> None:
        """`lit` ⇔ lo ≤ expr ≤ hi, while every literal in `enforce` holds."""
        self.m.add_linear_constraint(expr, lo, hi).only_enforce_if(lit)
        self.m.add_linear_expression_in_domain(
            expr, cp_model.Domain.from_intervals([[lo, hi]]).complement()
        ).only_enforce_if([~lit, *enforce])
        for e in enforce:
            self.m.add_implication(lit, e)

    def _reify_eq(self, lit, expr, value: int, enforce: list) -> None:
        self.m.add(expr == value).only_enforce_if(lit)
        self.m.add(expr != value).only_enforce_if([~lit, *enforce])
        for e in enforce:
            self.m.add_implication(lit, e)

    def _add(self, coef: int, lit) -> None:
        if coef:
            self.terms.append(coef * lit)

    # -- model ----------------------------------------------------------------

    def build(self) -> None:
        fixed_spans = _merge([
            (max(0, f.start), min(self.H, f.end))
            for f in self.inp.fixed
            if f.end > 0 and f.start < self.H and f.end > f.start
        ])
        self.intervals = [
            self.m.new_fixed_size_interval_var(s, e - s, f"fixed{n}")
            for n, (s, e) in enumerate(fixed_spans)
        ]
        for task in self.inp.tasks:
            tv = self._task_vars(task)
            if tv is not None:
                self.vars[task.key] = tv
                self.intervals.append(tv.interval)

        neighbour_mode = self.inp.neighbour_mode
        if neighbour_mode == "auto":
            neighbour_mode = "candidate" if len(self.vars) > NEIGHBOUR_TASK_LIMIT else "all"
        self.neighbour_mode = neighbour_mode

        for tv in self.vars.values():
            self._predecessor(tv)
            self._forbidden(tv)
            self._absent_cost(tv)
            self._stability(tv)
            self._static_costs(tv)
            if neighbour_mode == "candidate":
                tv.candidate_days = self._candidate_days(tv)
        self._neighbours()
        self._day_load()
        self._capacity()
        if self.inp.commute is not None:
            self._commute()
        self.m.add_no_overlap(self.intervals)
        self.m.minimize(cp_model.LinearExpr.sum(self.terms) + self.constant)

    def _task_vars(self, task: SolverTask) -> _TaskVars | None:
        lo = -(-max(task.earliest, 0) // TASK_GRID) * TASK_GRID
        hi = (self.H - task.dur) // TASK_GRID * TASK_GRID
        spans = [[lo, hi]] if lo <= hi else []
        current = task.current
        # A slot off the 15-minute grid (placed by hand, or by the old planner)
        # may stay where it is; any move lands on the grid.
        off_grid = current is not None and 0 <= current <= self.H - task.dur and (
            current < lo or current % TASK_GRID != 0
        )
        if off_grid and not (lo <= current <= hi and current % TASK_GRID == 0):
            spans.append([current, current])
            spans.sort()
        if not spans:
            return None
        key = task.key
        start = self.m.new_int_var_from_domain(cp_model.Domain.from_intervals(spans), f"s_{key}")
        quarter = self.m.new_int_var(spans[0][0] // TASK_GRID, spans[-1][1] // TASK_GRID, f"q_{key}")
        if off_grid:
            at_current = self._bool(f"offgrid_{key}")
            self.m.add(start == current).only_enforce_if(at_current)
            self.m.add(start == TASK_GRID * quarter).only_enforce_if(~at_current)
        else:
            self.m.add(start == TASK_GRID * quarter)
        present = self._bool(f"p_{key}")
        interval = self.m.new_optional_fixed_size_interval_var(start, task.dur, present, f"iv_{key}")
        # An absent task parks at its first allowed start so every derived
        # literal stays determined.
        self.m.add(start == spans[0][0]).only_enforce_if(~present)
        return _TaskVars(task, spans, start, present, interval)

    def _predecessor(self, tv: _TaskVars) -> None:
        pred_key = tv.task.predecessor
        if pred_key is None:
            return
        pred = self.vars.get(pred_key)
        if pred is None:
            if any(t.key == pred_key for t in self.inp.tasks):
                self.m.add(tv.present == 0)
            return
        self.m.add_implication(tv.present, pred.present)
        self.m.add(tv.start >= pred.start + pred.task.dur).only_enforce_if(tv.present)

    def _forbidden(self, tv: _TaskVars) -> None:
        for n, (fs, fe) in enumerate(tv.task.forbidden):
            if fe <= fs:
                continue
            before = self._bool(f"fb_{tv.task.key}_{n}")
            self.m.add(tv.start + tv.task.dur <= fs).only_enforce_if([before, tv.present])
            self.m.add(tv.start >= fe).only_enforce_if([~before, tv.present])

    def _absent_cost(self, tv: _TaskVars) -> None:
        task = tv.task
        cost = _cost(task.defer_utility) if task.deferrable else UNPLACED_COST
        # cost · (1 − present)
        self.constant += cost
        self._add(-cost, tv.present)

    def _stability(self, tv: _TaskVars) -> None:
        task = tv.task
        if task.current is None or not task.stability or not tv.allows(task.current):
            return
        stay = self._bool(f"stay_{task.key}")
        self.m.add(tv.start == task.current).only_enforce_if(stay)
        self.m.add_implication(stay, tv.present)
        self.m.add(tv.start != task.current).only_enforce_if([~stay, tv.present])
        cost = round(task.stability * SCALE)
        self.constant += cost
        self._add(-cost, stay)

    def _gated(self, name: str, expr, lo: int, hi: int, present) -> cp_model.IntVar:
        """A variable equal to `expr` while `present`, else 0."""
        var = self.m.new_int_var(min(lo, 0), max(hi, 0), name)
        self.m.add(var == expr).only_enforce_if(present)
        self.m.add(var == 0).only_enforce_if(~present)
        return var

    def _weights(self, task: SolverTask) -> dict[str, float]:
        if not task.neutral:
            return self.w
        return {k: v for k, v in self.w.items() if k.startswith(("late:", "late_h:"))}

    def _static_costs(self, tv: _TaskVars) -> None:
        task, m, key = tv.task, self.m, tv.task.key
        wt, eff = task.work_type, task.effort
        start, present = tv.start, tv.present
        w = self._weights(task)

        start_table = [_cost(F.utility(F.start_hour_features(h, wt, eff), w)) for h in range(24)]
        end_table = [_cost(F.utility(F.end_hour_features(h), w)) for h in range(24)]
        tod = m.new_int_var(0, SLOTS_PER_DAY - 1, f"tod_{key}")
        m.add_modulo_equality(tod, start, SLOTS_PER_DAY)
        hour = m.new_int_var(0, 23, f"hour_{key}")
        m.add_division_equality(hour, tod, SLOTS_PER_HOUR)
        last = m.new_int_var(tv.lo + task.dur - 1, self.H - 1, f"last_{key}")
        m.add(last == start + task.dur - 1)
        etod = m.new_int_var(0, SLOTS_PER_DAY - 1, f"etod_{key}")
        m.add_modulo_equality(etod, last, SLOTS_PER_DAY)
        ehour = m.new_int_var(0, 23, f"ehour_{key}")
        m.add_division_equality(ehour, etod, SLOTS_PER_HOUR)
        hc = m.new_int_var(min(start_table), max(start_table), f"hc_{key}")
        m.add_element(hour, start_table, hc)
        ehc = m.new_int_var(min(end_table), max(end_table), f"ehc_{key}")
        m.add_element(ehour, end_table, ehc)
        hours = self._gated(
            f"hcost_{key}", hc + ehc,
            min(start_table) + min(end_table), max(start_table) + max(end_table), present,
        )
        self.terms.append(hours)
        tv.hour_tables = (start_table, end_table)

        # Day terms and day membership in one go: exactly one day literal is
        # true while present.
        today = self.inp.days[0]
        first_day, last_day = tv.lo // SLOTS_PER_DAY, tv.hi // SLOTS_PER_DAY
        day_table = tv.day_table
        for d in range(first_day, last_day + 1):
            lit = self._bool(f"on_{key}_{d}")
            m.add_linear_constraint(start, d * SLOTS_PER_DAY, (d + 1) * SLOTS_PER_DAY - 1).only_enforce_if(lit)
            tv.day_lits[d] = lit
            meetings = self.inp.meeting_load[d] if d < len(self.inp.meeting_load) else 0.0
            day_table[d] = _cost(F.utility(
                F.day_features(self.inp.days[d], today, wt, meetings), w
            ))
            self._add(day_table[d], lit)
        m.add(sum(tv.day_lits.values()) == present)

        end = start + task.dur
        end_lo, end_hi = tv.lo + task.dur, tv.hi + task.dur
        kind = task.deadline_kind
        if task.due < end_hi:
            flag_cost = _cost(self.w.get(F.late_flag_key(kind), 0.0))
            slot_cost = -self.w.get(F.late_hours_key(kind), 0.0) * SCALE / SLOTS_PER_HOUR
            if task.due < end_lo:
                self._add(flag_cost, present)
            else:
                late = self._bool(f"late_{key}")
                m.add(end >= task.due + 1).only_enforce_if(late)
                m.add(end <= task.due).only_enforce_if([~late, present])
                m.add_implication(late, present)
                self._add(flag_cost, late)
            over = m.new_int_var(end_lo - task.due, end_hi - task.due, f"over_{key}")
            m.add(over == end - task.due)
            raw = m.new_int_var(0, max(0, end_hi - task.due), f"lateraw_{key}")
            m.add_max_equality(raw, [0, over])
            late_slots = self._gated(f"lates_{key}", raw, 0, max(0, end_hi - task.due), present)
            # Rounded per slot; keep at least one unit so lateness never ties.
            per_slot = round(slot_cost)
            if slot_cost and not per_slot:
                per_slot = 1 if slot_cost > 0 else -1
            self._add(per_slot, late_slots)

        # Hours-to-deadline buckets as cumulative thresholds on the end.
        htd = [_cost(w.get(F.htd_key(b), 0.0)) for b in F.HTD_BUCKETS]
        self._add(htd[-1], present)
        for n in reversed(range(len(F.HTD_HOURS))):
            delta = htd[n] - htd[n + 1]
            threshold = task.due - F.HTD_HOURS[n] * SLOTS_PER_HOUR
            if not delta or threshold >= end_hi:
                continue
            if threshold < end_lo:
                self._add(delta, present)
                continue
            lit = self._bool(f"htd_{key}_{n}")
            self._reify_range(lit, end, threshold + 1, end_hi, [present])
            self._add(delta, lit)

        # Defaults the neighbour literals correct: no neighbour, gap ≥ 15 min.
        for side in F.SIDES:
            none = F.neighbour_features(side, None, label=task.label, work_type=wt, effort=eff)
            gap = F.gap_features(side, None)
            self._add(_cost(F.utility({**none, **gap}, w)), present)

    def _candidate_days(self, tv: _TaskVars, count: int = CANDIDATE_DAYS) -> set[int]:
        start_table, end_table = tv.hour_tables
        best_hour = min(start_table) + min(end_table)
        ranked = sorted(
            tv.day_table, key=lambda d: tv.day_table[d] + best_hour + (
                UNPLACED_COST if d * SLOTS_PER_DAY + SLOTS_PER_DAY // 2 + tv.task.dur > tv.task.due
                and tv.task.deadline_kind != "soft" else 0
            ),
        )
        days = set(ranked[:count])
        for slot in (tv.task.current, tv.task.hint):
            if slot is not None and tv.allows(slot):
                days.add(slot // SLOTS_PER_DAY)
        return days

    # -- gaps and neighbours ----------------------------------------------------

    def _neighbour_cost(self, task: SolverTask, side: str, item) -> int:
        neighbour = F.Neighbour(
            kind=item.kind, gap_min=0, label=item.label,
            work_type=item.work_type, effort=item.effort,
        )
        kw = {"label": task.label, "work_type": task.work_type, "effort": task.effort}
        w = self._weights(task)
        chosen = F.utility(F.neighbour_features(side, neighbour, **kw), w)
        none = F.utility(F.neighbour_features(side, None, **kw), w)
        return _cost(chosen - none)

    def _gap_delta(self, side: str, slots: int, task: SolverTask) -> int:
        w = self._weights(task)
        chosen = F.utility(F.gap_features(side, slots * SLOT_MINUTES), w)
        default = F.utility(F.gap_features(side, None), w)
        return _cost(chosen - default)

    def _allowed(self, tv: _TaskVars, slot: int) -> bool:
        return tv.candidate_days is None or slot // SLOTS_PER_DAY in tv.candidate_days

    def _neighbours(self) -> None:
        tvs = list(self.vars.values())
        for tv in tvs:
            task = tv.task
            end = tv.start + task.dur
            end_lo, end_hi = tv.lo + task.dur, tv.hi + task.dur
            for n, item in enumerate(self.inp.fixed):
                # Item after the task: its start within 60 min of the task end.
                if end_lo <= item.start <= end_hi + NEIGHBOUR_SLOTS and self._allowed(tv, item.start):
                    self._pair_side(
                        tv, "next", item, end, item.start, f"{task.key}_fn{n}",
                        lo=item.start - NEIGHBOUR_SLOTS, hi=item.start, value_of=lambda k, s=item.start: s - k,
                        enforce=[tv.present],
                    )
                # Item before the task: its end within 60 min of the task start.
                if tv.lo - NEIGHBOUR_SLOTS <= item.end <= tv.hi and self._allowed(tv, item.end):
                    self._pair_side(
                        tv, "prev", item, tv.start, item.end, f"{task.key}_fp{n}",
                        lo=item.end, hi=item.end + NEIGHBOUR_SLOTS, value_of=lambda k, e=item.end: e + k,
                        enforce=[tv.present],
                    )
        for a in tvs:
            for b in tvs:
                if a is b:
                    continue
                self._task_pair(a, b)
        for tv in tvs:
            for side in F.SIDES:
                lits = tv.side_lits[side]
                if not lits:
                    continue
                chosen = [nb for nb, _ in lits]
                within = [w for _, w in lits]
                any_within = self._bool(f"any_{tv.task.key}_{side}")
                self.m.add_max_equality(any_within, within)
                self.m.add(sum(chosen) == any_within)

    def _pair_side(self, tv, side, item, expr, anchor, name, *, lo, hi, value_of, enforce) -> None:
        """Literals for one fixed item next to a movable task. `expr` is the
        task boundary facing the item (end for "next", start for "prev"),
        `[lo, hi]` the range of `expr` that puts the item within 60 min, and
        `value_of(k)` the boundary value for a k-slot gap."""
        within = self._bool(f"w_{name}")
        self._reify_range(within, expr, lo, hi, enforce)
        chosen = self._bool(f"nb_{name}")
        self.m.add_implication(chosen, within)
        tv.side_lits[side].append((chosen, within))
        self._add(self._neighbour_cost(tv.task, side, item), chosen)
        for k in range(3):
            delta = self._gap_delta(side, k, tv.task)
            if not delta:
                continue
            gap = self._bool(f"g{k}_{name}")
            self._reify_eq(gap, expr, value_of(k), enforce)
            self.m.add_implication(gap, chosen)
            self._add(delta, gap)

    def _task_pair(self, a: _TaskVars, b: _TaskVars) -> None:
        """b starts within 60 min after a ends: a's "next" and b's "prev"."""
        a_end_lo, a_end_hi = a.lo + a.task.dur, a.hi + a.task.dur
        if b.hi < a_end_lo or b.lo > a_end_hi + NEIGHBOUR_SLOTS:
            return
        if a.candidate_days is not None and b.candidate_days is not None:
            if not a.candidate_days & b.candidate_days:
                return
        name = f"{a.task.key}_{b.task.key}"
        diff = b.start - (a.start + a.task.dur)
        enforce = [a.present, b.present]
        within = self._bool(f"tw_{name}")
        self._reify_range(within, diff, 0, NEIGHBOUR_SLOTS, enforce)
        b_item = FixedItem(0, 0, "task", b.task.label, b.task.work_type, b.task.effort)
        a_item = FixedItem(0, 0, "task", a.task.label, a.task.work_type, a.task.effort)
        nb_a = self._bool(f"tnn_{name}")
        nb_b = self._bool(f"tnp_{name}")
        self.m.add_implication(nb_a, within)
        self.m.add_implication(nb_b, within)
        a.side_lits["next"].append((nb_a, within))
        b.side_lits["prev"].append((nb_b, within))
        self._add(self._neighbour_cost(a.task, "next", b_item), nb_a)
        self._add(self._neighbour_cost(b.task, "prev", a_item), nb_b)
        for k in range(3):
            delta = self._gap_delta("next", k, a.task) + self._gap_delta("prev", k, b.task)
            if not delta:
                continue
            gap = self._bool(f"tg{k}_{name}")
            self._reify_eq(gap, diff, k, enforce)
            self.m.add_implication(gap, nb_a)
            self.m.add_implication(gap, nb_b)
            self._add(delta, gap)

    # -- day load and capacity --------------------------------------------------

    def _day_load(self) -> None:
        loads = [_cost(self.w.get(F.load_key(b), 0.0)) for b in F.LOAD_BUCKETS]
        for tv in self.vars.values():
            if not tv.task.neutral:
                self._add(loads[0], tv.present)
        deltas = [loads[n + 1] - loads[n] for n in range(len(F.LOAD_MINUTES))]
        for d in range(len(self.inp.days)):
            members = [(tv, tv.day_lits[d]) for tv in self.vars.values() if d in tv.day_lits]
            if not members:
                continue
            base = round(self.inp.fixed_load[d]) if d < len(self.inp.fixed_load) else 0
            top = base + sum(tv.task.dur * SLOT_MINUTES for tv, _ in members)
            load = self.m.new_int_var(base, top, f"load_{d}")
            self.m.add(load == base + sum(tv.task.dur * SLOT_MINUTES * lit for tv, lit in members))
            for n, threshold in enumerate(F.LOAD_MINUTES):
                delta = deltas[n]
                if not delta or top < threshold:
                    continue
                # A neutral task still adds to the day's load (others feel it)
                # but carries no load cost of its own.
                charged = [(tv, lit) for tv, lit in members if not tv.task.neutral]
                if base >= threshold:
                    for _, lit in charged:
                        self._add(delta, lit)
                    continue
                over = self._bool(f"ge_{d}_{n}")
                self.m.add(load >= threshold).only_enforce_if(over)
                self.m.add(load <= threshold - 1).only_enforce_if(~over)
                for tv, lit in charged:
                    both = self._bool(f"ld_{tv.task.key}_{d}_{n}")
                    self.m.add_implication(both, lit)
                    self.m.add_implication(both, over)
                    self.m.add_bool_or([~lit, ~over, both])
                    self._add(delta, both)

    def _capacity(self) -> None:
        for n, bound in enumerate(self.inp.capacity):
            members = [
                tv for tv in self.vars.values()
                if tv.task.deferrable and tv.task.due <= bound.due_by
            ]
            if not members:
                continue
            slack = self.m.new_int_var(0, sum(tv.task.dur * SLOT_MINUTES for tv in members), f"slack_{n}")
            deferred = sum(tv.task.dur * SLOT_MINUTES * (1 - tv.present) for tv in members)
            self.m.add(deferred - slack <= round(bound.capacity_min))
            self._add(CAPACITY_SLACK_COST, slack)



    # -- commute ----------------------------------------------------------------

    def _commute(self) -> None:
        C = self.inp.commute
        m, B = self.m, C.buffer
        n_locations = 1 + max(
            [C.start_location, C.bike_start, *C.task_locations.values(),
             *(n.loc for n in C.fixed), *(a for pair in C.travel for a in pair)]
        )
        legs_max = 2 * B + max(
            [UNROUTABLE_SLOTS, *(t for pair in C.travel.values() for t in pair if t is not None)]
        )
        rain = [_cost(u) for u in C.ride_hour_utility] or [0]
        hours = (len(self.inp.days) + 1) * 24
        rain = (rain + [rain[-1]] * hours)[:hours]
        after_physical = _cost(C.after_physical_utility)

        for tv in self.vars.values():
            if tv.task.key not in C.task_locations:
                continue
            tv.route_days = self._candidate_days(tv, C.candidate_days)
            for d, lit in tv.day_lits.items():
                if d not in tv.route_days:
                    m.add(lit == 0)

        bike_prev = None
        for d in range(len(self.inp.days)):
            lo, hi = d * SLOTS_PER_DAY, (d + 1) * SLOTS_PER_DAY
            t_lo, t_hi = lo - 12 * SLOTS_PER_HOUR, hi + 12 * SLOTS_PER_HOUR
            nodes: list[dict] = []

            def node(kind, key_in, key_out, loc, **kw):
                nodes.append({"kind": kind, "key_in": key_in, "key_out": key_out, "loc": loc,
                              "present": None, "physical": False, **kw})
                return len(nodes) - 1

            s_idx = node("S", C.start_key if d == 0 else HOME, C.start_key if d == 0 else HOME,
                         C.start_location if d == 0 else 0)
            e_idx = node("E", HOME, HOME, 0)
            for rn in C.fixed:
                if lo <= rn.start < hi:
                    node("item", rn.key_in, rn.key_out, rn.loc, start=rn.start, end=rn.end,
                         physical=rn.physical)
            for key, loc in C.task_locations.items():
                tv = self.vars.get(key)
                if tv is None or d not in tv.route_days or d not in tv.day_lits:
                    continue
                anchor = C.task_anchors.get(key, f"task:{key}")
                node("task", anchor, anchor, loc, start=tv.start, end=tv.start + tv.task.dur,
                     present=tv.day_lits[d], physical=C.task_physical.get(key, False), task=key)
            # Going home between two away items needs one stop per gap, so a
            # day with n places away can use at most n − 1 (capped).
            away = sum(1 for nd in nodes if nd["kind"] in ("item", "task", "S") and nd["loc"] != 0)
            stops = []
            for k in range(min(C.home_stops, max(0, away - 1))):
                stops.append(node("stop", HOME, HOME, 0, present=self._bool(f"stop_{d}_{k}")))

            for n_idx, nd in enumerate(nodes):
                tag = f"{d}_{n_idx}"
                nd["arr"] = m.new_int_var(t_lo, t_hi, f"arr_{tag}")
                nd["dep"] = m.new_int_var(t_lo, t_hi, f"dep_{tag}")
                nd["bike"] = m.new_int_var(0, n_locations - 1, f"bike_{tag}")
                nd["ride"] = self._bool(f"ride_{tag}")
                enforce = [nd["present"]] if nd["present"] is not None else []
                m.add(nd["arr"] <= nd["dep"]).only_enforce_if(enforce)
                if nd["kind"] in ("item", "task"):
                    m.add(nd["arr"] <= nd["start"]).only_enforce_if(enforce)
                    m.add(nd["dep"] >= nd["end"]).only_enforce_if(enforce)
            start_node = nodes[s_idx]
            m.add(start_node["dep"] >= (max(C.available_from, C.now) if d == 0 else lo))
            m.add(start_node["bike"] == (C.bike_start if bike_prev is None else bike_prev))
            m.add(start_node["ride"] == 0)
            for a, b in zip(stops, stops[1:]):
                m.add_implication(nodes[b]["present"], nodes[a]["present"])
                m.add(nodes[b]["arr"] >= nodes[a]["dep"]).only_enforce_if(
                    [nodes[a]["present"], nodes[b]["present"]]
                )

            arcs: dict[tuple[int, int], cp_model.IntVar] = {}
            for i, ni in enumerate(nodes):
                for j, nj in enumerate(nodes):
                    if i == j or i == e_idx or j == s_idx:
                        continue
                    if not self._route_arc_allowed(ni, nj):
                        continue
                    arcs[(i, j)] = self._bool(f"arc_{d}_{i}_{j}")
            circuit = [(i, j, lit) for (i, j), lit in arcs.items()]
            circuit.append((e_idx, s_idx, m.new_constant(1)))
            circuit += [(i, i, ~nd["present"]) for i, nd in enumerate(nodes) if nd["present"] is not None]
            m.add_circuit(circuit)

            for j, nj in enumerate(nodes):
                if j == s_idx:
                    continue
                self._arrival_leg(d, j, nj, nodes, arcs, legs_max, rain, after_physical, t_lo, t_hi)

            # Away from home: the day's span minus time spent at home nodes.
            away = nodes[e_idx]["arr"] - start_node["dep"]
            for i, nd in enumerate(nodes):
                if nd["loc"] != 0 or nd["kind"] in ("S", "E"):
                    continue
                stay = m.new_int_var(0, t_hi - t_lo, f"stay_{d}_{i}")
                enforce = [nd["present"]] if nd["present"] is not None else []
                m.add(stay == nd["dep"] - nd["arr"]).only_enforce_if(enforce)
                if nd["present"] is not None:
                    m.add(stay == 0).only_enforce_if(~nd["present"])
                away -= stay
            self.terms.append(AWAY_COST * away)
            for k in stops:
                self._add(EXTRA_TRIP_COST, nodes[k]["present"])
            end_bike = nodes[e_idx]["bike"]
            overnight = self._bool(f"bike_away_{d}")
            m.add(end_bike != 0).only_enforce_if(overnight)
            m.add(end_bike == 0).only_enforce_if(~overnight)
            self._add(BIKE_AWAY_NIGHT_COST, overnight)
            bike_prev = end_bike
            self.routes[d] = {"nodes": nodes, "arcs": arcs, "start": s_idx, "end": e_idx,
                              "stops": stops}

    @staticmethod
    def _route_arc_allowed(ni: dict, nj: dict) -> bool:
        kinds = (ni["kind"], nj["kind"])
        if kinds == ("S", "stop") or kinds == ("stop", "E") or kinds == ("stop", "stop"):
            return False
        # A home stop only makes sense between two places away from home.
        if ni["kind"] == "stop" and nj["loc"] == 0:
            return False
        if nj["kind"] == "stop" and ni["loc"] == 0:
            return False
        if ni["kind"] == "item" and nj["kind"] == "item" and ni["end"] > nj["start"]:
            return False
        return True

    def _arrival_leg(self, d, j, nj, nodes, arcs, legs_max, rain, after_physical, t_lo, t_hi) -> None:
        C, m, B = self.inp.commute, self.m, self.inp.commute.buffer
        present = nj["present"]
        enforce = [present] if present is not None else []
        duration_terms, cost_terms = [], []
        nj["choices"] = {}
        for (i, jj), arc in arcs.items():
            if jj != j:
                continue
            ni = nodes[i]
            pair = (ni["key_out"], nj["key_in"])
            if ni["loc"] == nj["loc"]:
                m.add(nj["ride"] == 0).only_enforce_if(arc)
                m.add(nj["bike"] == ni["bike"]).only_enforce_if(arc)
                continue
            bike_t, transit_t = C.travel.get((ni["loc"], nj["loc"]), (None, None))
            ride = self._bool(f"y_{d}_{i}_{j}")
            walk = self._bool(f"z_{d}_{i}_{j}")
            m.add_bool_and([arc, nj["ride"]]).only_enforce_if(ride)
            m.add_bool_or([~arc, ~nj["ride"], ride])
            m.add_bool_and([arc, ~nj["ride"]]).only_enforce_if(walk)
            m.add_bool_or([~arc, nj["ride"], walk])
            # Ridden: the bike must be where the leg starts, and ends up where
            # it arrives. Otherwise it stays wherever it was.
            m.add(ni["bike"] == ni["loc"]).only_enforce_if(ride)
            m.add(nj["bike"] == nj["loc"]).only_enforce_if(ride)
            m.add(nj["bike"] == ni["bike"]).only_enforce_if(walk)
            transit_slots = (transit_t if transit_t is not None else UNROUTABLE_SLOTS) + 2 * B
            transit_cost = _cost(C.leg_utility.get((ni["loc"], nj["loc"], "transit"), 0.0))
            if transit_t is None:
                transit_cost += UNROUTABLE_COST
            duration_terms.append(transit_slots * arc)
            cost_terms.append(transit_cost * arc)
            if bike_t is None:
                m.add(ride == 0)
            else:
                bike_slots = bike_t + 2 * B
                bike_cost = _cost(C.leg_utility.get((ni["loc"], nj["loc"], "bicycling"), 0.0))
                if ni["physical"]:
                    bike_cost += after_physical
                duration_terms.append((bike_slots - transit_slots) * ride)
                cost_terms.append((bike_cost - transit_cost) * ride)
            pin = C.pins.get(pair)
            if pin is not None:
                cost_terms.append(PIN_COST * (walk if pin == "bicycling" else ride))
            current = C.current.get(pair)
            if current is not None and current[1] - C.now < MODE_CHANGE_WINDOW:
                cost_terms.append(MODE_CHANGE_COST * (walk if current[0] == "bicycling" else ride))
            nj["choices"][i] = (arc, ride)
        tag = f"{d}_{j}"
        shortfall = m.new_int_var(0, legs_max, f"sf_{tag}")
        size = m.new_int_var(0, legs_max, f"legsize_{tag}")
        start = m.new_int_var(t_lo, t_hi, f"legstart_{tag}")
        end = m.new_int_var(t_lo, t_hi, f"legend_{tag}")
        m.add(end == start + size)
        m.add(size == sum(duration_terms) - shortfall).only_enforce_if(enforce)
        # Only legs with a length take part in the no-overlap: CP-SAT can't
        # place a zero-length interval strictly inside another one, and a leg
        # squeezed to nothing between back-to-back events would land there.
        moving = self._bool(f"moving_{tag}")
        m.add(size >= 1).only_enforce_if(moving)
        m.add(size == 0).only_enforce_if(~moving)
        if present is not None:
            m.add_implication(moving, present)
        self.intervals.append(m.new_optional_interval_var(start, size, end, moving, f"leg_{tag}"))
        for (i, jj), arc in arcs.items():
            if jj == j:
                m.add(start >= nodes[i]["dep"]).only_enforce_if(arc)
        m.add(end <= nj["arr"]).only_enforce_if(enforce)
        self.terms.append(SHORTFALL_COST * shortfall)
        self.terms.extend(cost_terms)
        # Weather at the hour the ride starts.
        ride_start = m.new_int_var(0, len(rain) * SLOTS_PER_HOUR - 1, f"ridestart_{tag}")
        m.add(ride_start == start).only_enforce_if([*enforce, nj["ride"]])
        m.add(ride_start == 0).only_enforce_if(~nj["ride"])
        hour = m.new_int_var(0, len(rain) - 1, f"ridehour_{tag}")
        m.add_division_equality(hour, ride_start, SLOTS_PER_HOUR)
        weather = m.new_int_var(min(rain), max(rain), f"rain_{tag}")
        m.add_element(hour, rain, weather)
        weather_on = m.new_int_var(min(0, min(rain)), max(0, max(rain)), f"rainon_{tag}")
        m.add(weather_on == weather).only_enforce_if(nj["ride"])
        m.add(weather_on == 0).only_enforce_if(~nj["ride"])
        self.terms.append(weather_on)
        nj.update(leg_start=start, leg_end=end, shortfall=shortfall)

    def route_hint(self) -> dict:
        """Discrete routing choices for the warm start: visit the day's items
        in time order, go home between two away items when today's layover
        rule would, ride when the bike is at hand and riding is cheaper."""
        C = self.inp.commute
        fixes: dict = {}
        bike = C.bike_start
        for d, route in self.routes.items():
            nodes, arcs = route["nodes"], route["arcs"]
            visit = []
            for i, nd in enumerate(nodes):
                if nd["kind"] == "item":
                    visit.append((nd["start"], i))
                elif nd["kind"] == "task":
                    tv = self.vars[nd["task"]]
                    hint = tv.task.hint
                    present = hint is not None and tv.allows(hint) and hint // SLOTS_PER_DAY == d
                    fixes[nd["present"]] = int(present)
                    if present:
                        visit.append((hint, i))
            free_stops = list(route["stops"])
            path = [route["start"]]
            where = nodes[route["start"]]["loc"]
            ready = max(C.available_from, C.now) if d == 0 else d * SLOTS_PER_DAY
            for start, i in sorted(visit):
                nd = nodes[i]
                if where != 0 and nd["loc"] not in (0, where) and free_stops:
                    out = self._slots_home(where, 0)
                    back = self._slots_home(0, nd["loc"])
                    if start - ready >= out + back + SLOTS_PER_HOUR:
                        stop = free_stops.pop(0)
                        if (path[-1], stop) in arcs and (stop, i) in arcs:
                            path.append(stop)
                where = nd["loc"]
                path.append(i)
                if nd["kind"] == "task":
                    task = self.vars[nd["task"]].task
                    ready = task.hint + task.dur
                else:
                    ready = nd["end"]
            path.append(route["end"])
            for k in route["stops"]:
                fixes[nodes[k]["present"]] = int(k in path)
            for a, b in zip(path, path[1:]):
                if (a, b) not in arcs:
                    return {}
                fixes[arcs[(a, b)]] = 1
                na, nb = nodes[a], nodes[b]
                ride = 0
                if na["loc"] != nb["loc"]:
                    bike_t, transit_t = C.travel.get((na["loc"], nb["loc"]), (None, None))
                    if bike_t is not None and bike == na["loc"]:
                        ride_cost = _cost(C.leg_utility.get((na["loc"], nb["loc"], "bicycling"), 0.0))
                        transit_cost = _cost(C.leg_utility.get((na["loc"], nb["loc"], "transit"), 0.0))
                        if transit_t is None:
                            transit_cost += UNROUTABLE_COST
                        ride = int(ride_cost < transit_cost)
                    if ride:
                        bike = nb["loc"]
                fixes[nb["ride"]] = ride
        return fixes

    def _slots_home(self, a: int, b: int) -> int:
        bike_t, transit_t = self.inp.commute.travel.get((a, b), (None, None))
        options = [t for t in (bike_t, transit_t) if t is not None]
        return (min(options) if options else UNROUTABLE_SLOTS) + 2 * self.inp.commute.buffer

    def extract_routes(self, solver: cp_model.CpSolver) -> list[RouteLeg]:
        C = self.inp.commute
        out: list[RouteLeg] = []
        for d, route in self.routes.items():
            nodes, arcs = route["nodes"], route["arcs"]
            succ = {i: j for (i, j), lit in arcs.items() if solver.value(lit)}
            i = route["start"]
            for _ in range(len(nodes)):
                j = succ.get(i)
                if j is None:
                    break
                ni, nj = nodes[i], nodes[j]
                if ni["loc"] != nj["loc"]:
                    ride = bool(solver.value(nj["ride"]))
                    bike_t, transit_t = C.travel.get((ni["loc"], nj["loc"]), (None, None))
                    mode = "bicycling" if ride else ("transit" if transit_t is not None else "failed")
                    travel = bike_t if ride else (transit_t if transit_t is not None else UNROUTABLE_SLOTS)
                    out.append(RouteLeg(
                        day=d,
                        origin_key=ni["key_out"],
                        dest_key=nj["key_in"],
                        origin_loc=ni["loc"],
                        dest_loc=nj["loc"],
                        mode=mode,
                        start=solver.value(nj["leg_start"]),
                        end=solver.value(nj["leg_end"]),
                        travel=travel,
                        shortfall=solver.value(nj["shortfall"]),
                        bike_before=solver.value(ni["bike"]),
                        bike_after=solver.value(nj["bike"]),
                        pinned=(ni["key_out"], nj["key_in"]) in C.pins,
                    ))
                if j == route["end"]:
                    break
                i = j
        return out


def _hint_values(builder: _Builder, inp: SolveInput) -> tuple[list[int], float] | None:
    """Complete the warm start into a full assignment: fix every task to its
    hinted start (or absent) — and, with commute, the simulated route — and
    let a short solve settle the rest."""
    attempts = [builder.route_hint(), {}] if inp.commute is not None else [{}]
    for fixes in attempts:
        probe = builder.m.clone()
        for tv in builder.vars.values():
            start = probe.get_int_var_from_proto_index(tv.start.index)
            present = probe.get_bool_var_from_proto_index(tv.present.index)
            hint = tv.task.hint
            if hint is None or not tv.allows(hint):
                probe.add(present == 0)
            else:
                probe.add(present == 1)
                probe.add(start == hint)
        for var, value in fixes.items():
            probe.add(probe.get_bool_var_from_proto_index(var.index) == value)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = max(1.0, inp.time_limit / 2)
        solver.parameters.random_seed = inp.seed
        status = solver.solve(probe)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return list(solver.response_proto.solution), solver.objective_value
        if fixes:
            log.info("solver · simulated route rejected (%s); completing without it",
                     solver.status_name(status))
    return None


def solve(inp: SolveInput) -> SolveResult:
    started = time.monotonic()
    builder = _Builder(inp)
    builder.build()
    starts: dict[str, int | None] = {t.key: None for t in inp.tasks}

    hint = _hint_values(builder, inp)
    hint_objective = None
    if hint is not None:
        values, hint_objective = hint
        model = builder.m
        for index, value in enumerate(values):
            model.add_hint(model.get_int_var_from_proto_index(index), value)
    else:
        log.warning("solver · warm start infeasible; solving without a hint")

    solver = cp_model.CpSolver()
    solver.parameters.num_workers = inp.workers
    solver.parameters.max_time_in_seconds = inp.time_limit
    solver.parameters.random_seed = inp.seed
    status = solver.solve(builder.m)
    status_name = solver.status_name(status)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) and (
        hint_objective is None or solver.objective_value <= hint_objective
    ):
        for key, tv in builder.vars.items():
            if solver.value(tv.present):
                starts[key] = solver.value(tv.start)
        return SolveResult(
            starts=starts,
            status=status_name,
            objective=solver.objective_value / SCALE,
            bound=solver.best_objective_bound / SCALE,
            hint_objective=hint_objective / SCALE if hint_objective is not None else None,
            wall_time=time.monotonic() - started,
            neighbour_mode=builder.neighbour_mode,
            routes=builder.extract_routes(solver) if inp.commute is not None else None,
        )

    if hint is not None:
        for key, tv in builder.vars.items():
            if tv.task.hint is not None and tv.allows(tv.task.hint):
                starts[key] = tv.task.hint
    return SolveResult(
        starts=starts,
        status=status_name,
        objective=hint_objective / SCALE if hint_objective is not None else None,
        bound=None,
        hint_objective=hint_objective / SCALE if hint_objective is not None else None,
        wall_time=time.monotonic() - started,
        neighbour_mode=builder.neighbour_mode,
        from_hint=True,
        ok=hint is not None,
    )
