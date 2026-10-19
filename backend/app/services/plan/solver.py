"""CP-SAT model of the task schedule — pure, no I/O.

Time is a grid of 5-minute slots, `t = 0` at local midnight of the first
horizon day. Hard constraints are only what is physically or logically
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

The warm start (`SolverTask.hint`) is completed into a full assignment before
the solve — CP-SAT ignores an incomplete hint — so the result is never worse
than the hinted schedule.
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
NEIGHBOUR_SLOTS = F.NEIGHBOUR_MINUTES // SLOT_MINUTES
SCALE = 1000
UNPLACED_COST = 10**8
# Per minute of deferred work beyond the expected future capacity.
CAPACITY_SLACK_COST = 2 * SCALE
NEIGHBOUR_TASK_LIMIT = 50
CANDIDATE_DAYS = 3


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
        return any(lo <= start <= hi for lo, hi in self.spans)


class _Builder:
    def __init__(self, inp: SolveInput):
        self.inp = inp
        self.m = cp_model.CpModel()
        self.w = inp.weights
        self.H = inp.horizon
        self.vars: dict[str, _TaskVars] = {}
        self.terms: list = []
        self.constant = 0

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
        intervals = [
            self.m.new_fixed_size_interval_var(s, e - s, f"fixed{n}")
            for n, (s, e) in enumerate(fixed_spans)
        ]
        for task in self.inp.tasks:
            tv = self._task_vars(task)
            if tv is not None:
                self.vars[task.key] = tv
                intervals.append(tv.interval)
        self.m.add_no_overlap(intervals)

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
        self.m.minimize(cp_model.LinearExpr.sum(self.terms) + self.constant)

    def _task_vars(self, task: SolverTask) -> _TaskVars | None:
        lo, hi = max(task.earliest, 0), self.H - task.dur
        spans = [[lo, hi]] if lo <= hi else []
        if task.current is not None and 0 <= task.current < lo and task.current <= hi:
            spans.insert(0, [task.current, task.current])
        if not spans:
            return None
        key = task.key
        start = self.m.new_int_var_from_domain(cp_model.Domain.from_intervals(spans), f"s_{key}")
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

    def _static_costs(self, tv: _TaskVars) -> None:
        task, m, key = tv.task, self.m, tv.task.key
        wt, eff = task.work_type, task.effort
        start, present = tv.start, tv.present

        start_table = [_cost(F.utility(F.start_hour_features(h, wt, eff), self.w)) for h in range(24)]
        end_table = [_cost(F.utility(F.end_hour_features(h), self.w)) for h in range(24)]
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
            day_table[d] = _cost(F.utility(
                F.day_features(self.inp.days[d], today, wt), self.w
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
        htd = [_cost(self.w.get(F.htd_key(b), 0.0)) for b in F.HTD_BUCKETS]
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
            self._add(_cost(F.utility({**none, **gap}, self.w)), present)

    def _candidate_days(self, tv: _TaskVars) -> set[int]:
        start_table, end_table = tv.hour_tables
        best_hour = min(start_table) + min(end_table)
        ranked = sorted(
            tv.day_table, key=lambda d: tv.day_table[d] + best_hour + (
                UNPLACED_COST if d * SLOTS_PER_DAY + SLOTS_PER_DAY // 2 + tv.task.dur > tv.task.due
                and tv.task.deadline_kind != "soft" else 0
            ),
        )
        days = set(ranked[:CANDIDATE_DAYS])
        if tv.task.current is not None:
            days.add(tv.task.current // SLOTS_PER_DAY)
        return days

    # -- gaps and neighbours ----------------------------------------------------

    def _neighbour_cost(self, task: SolverTask, side: str, item) -> int:
        neighbour = F.Neighbour(
            kind=item.kind, gap_min=0, label=item.label,
            work_type=item.work_type, effort=item.effort,
        )
        kw = {"label": task.label, "work_type": task.work_type, "effort": task.effort}
        chosen = F.utility(F.neighbour_features(side, neighbour, **kw), self.w)
        none = F.utility(F.neighbour_features(side, None, **kw), self.w)
        return _cost(chosen - none)

    def _gap_delta(self, side: str, slots: int) -> int:
        chosen = F.utility(F.gap_features(side, slots * SLOT_MINUTES), self.w)
        default = F.utility(F.gap_features(side, None), self.w)
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
            delta = self._gap_delta(side, k)
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
            delta = self._gap_delta("next", k) + self._gap_delta("prev", k)
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
                if base >= threshold:
                    for _, lit in members:
                        self._add(delta, lit)
                    continue
                over = self._bool(f"ge_{d}_{n}")
                self.m.add(load >= threshold).only_enforce_if(over)
                self.m.add(load <= threshold - 1).only_enforce_if(~over)
                for tv, lit in members:
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


def _hint_values(builder: _Builder, inp: SolveInput) -> tuple[list[int], float] | None:
    """Complete the warm start into a full assignment: fix every task to its
    hinted start (or absent) and let a short solve settle the rest."""
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
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = max(1.0, inp.time_limit / 2)
    solver.parameters.random_seed = inp.seed
    status = solver.solve(probe)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return list(solver.response_proto.solution), solver.objective_value


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
