"""CP-SAT schedule model on small deterministic instances (one worker)."""

from __future__ import annotations

from datetime import date, timedelta

from app.services.plan import solver as S

MONDAY = date(2026, 10, 19)
DAY = S.SLOTS_PER_DAY
HOUR = S.SLOTS_PER_HOUR


def at(day: int, hour: float) -> int:
    return day * DAY + round(hour * HOUR)


def solve(tasks, *, fixed=(), days=2, capacity=(), weights=None, **kw) -> S.SolveResult:
    inp = S.SolveInput(
        days=[MONDAY + timedelta(days=d) for d in range(days)],
        tasks=list(tasks),
        fixed=list(fixed),
        fixed_load=[0.0] * days,
        capacity=list(capacity),
        time_limit=kw.pop("time_limit", 10.0),
        workers=1,
        **({"weights": weights} if weights else {}),
        **kw,
    )
    result = S.solve(inp)
    assert result.ok
    return result


def task(key, dur_h=1.0, *, earliest=at(0, 8), due=at(1, 23), **kw) -> S.SolverTask:
    return S.SolverTask(key=key, dur=round(dur_h * HOUR), earliest=earliest, due=due, **kw)


def spans(result, tasks) -> dict[str, tuple[int, int]]:
    durations = {t.key: t.dur for t in tasks}
    return {k: (s, s + durations[k]) for k, s in result.starts.items() if s is not None}


def test_prior_places_inside_the_old_day_window():
    tasks = [task("a", 2)]
    start = solve(tasks).starts["a"]
    assert at(0, 10) <= start and start + 2 * HOUR <= at(0, 19)


def test_no_overlap_with_events_or_each_other():
    fixed = [S.FixedItem(at(0, 10), at(0, 19), "event")]
    tasks = [task("a", 1), task("b", 1), task("c", 1)]
    placed = spans(solve(tasks, fixed=fixed), tasks)
    assert len(placed) == 3
    for s, e in placed.values():
        assert e <= at(0, 10) or s >= at(0, 19)
    ordered = sorted(placed.values())
    assert all(a[1] <= b[0] for a, b in zip(ordered, ordered[1:]))


def test_gap_prior_keeps_a_buffer_when_there_is_room():
    fixed = [S.FixedItem(at(0, 12), at(0, 13), "event")]
    tasks = [task("a", 1, earliest=at(0, 10), due=at(0, 12))]
    start = solve(tasks, fixed=fixed).starts["a"]
    # Flush against the event at 12:00 would cost the 0-minute gap prior.
    assert start + HOUR <= at(0, 12) - 3


def test_predecessor_order_holds():
    tasks = [task("second", 1, predecessor="first"), task("first", 1)]
    result = solve(tasks)
    assert result.starts["second"] >= result.starts["first"] + HOUR


def test_hard_deadline_kept_while_an_on_time_slot_exists():
    fixed = [S.FixedItem(at(0, 9), at(0, 20), "event")]
    # Only 20:00–21:00 is on time — a poor hour, but lateness is worse.
    tasks = [task("a", 1, earliest=at(0, 9), due=at(0, 21), deadline_kind="hard")]
    start = solve(tasks, fixed=fixed).starts["a"]
    assert start + HOUR <= at(0, 21)


def test_soft_deadline_trades_lateness_for_a_better_hour():
    fixed = [S.FixedItem(at(0, 9), at(0, 20), "event")]
    tasks = [task("a", 1, earliest=at(0, 9), due=at(0, 21), deadline_kind="soft")]
    start = solve(tasks, fixed=fixed).starts["a"]
    assert start >= at(1, 10)


def test_lateness_only_when_unavoidable():
    fixed = [S.FixedItem(at(0, 8), at(0, 23), "event")]
    tasks = [task("a", 1, earliest=at(0, 8), due=at(0, 12), deadline_kind="firm")]
    start = solve(tasks, fixed=fixed).starts["a"]
    assert start >= at(0, 23)  # nothing fits before the deadline


def test_stability_prevents_gratuitous_moves():
    # Tuesday 14:00 is fine; Monday would be marginally better (days-from-now).
    tasks = [task("a", 1, current=at(1, 14), hint=at(1, 14), stability=1.0)]
    assert solve(tasks).starts["a"] == at(1, 14)


def test_user_anchor_holds_until_released():
    # Hand-placed at 20:00 — a −2 hour under the prior.
    anchored = [task("a", 1, current=at(0, 20), hint=at(0, 20), stability=20.0)]
    assert solve(anchored).starts["a"] == at(0, 20)
    released = [task("a", 1, current=at(0, 20), hint=at(0, 20), stability=1.0)]
    assert solve(released).starts["a"] != at(0, 20)


def test_current_slot_inside_the_lead_time_may_stay():
    tasks = [task("a", 1, earliest=at(0, 12), current=at(0, 11), hint=at(0, 11), stability=3.0)]
    assert solve(tasks).starts["a"] == at(0, 11)


def test_forbidden_span_is_avoided():
    tasks = [task("a", 1, forbidden=((at(0, 10), at(0, 19)),), due=at(0, 23))]
    start = solve(tasks).starts["a"]
    assert start + HOUR <= at(0, 10) or start >= at(0, 19)


def test_due_inside_horizon_is_placed_even_on_a_poor_slot():
    # Only the night is free; a task due inside the horizon still gets it.
    fixed = [S.FixedItem(at(0, 8), at(1, 23), "event")]
    tasks = [task("a", 1, earliest=at(0, 1), due=at(1, 23))]
    start = solve(tasks, fixed=fixed).starts["a"]
    assert start is not None and start + HOUR <= at(0, 8)


def test_unplaced_only_when_nothing_fits():
    fixed = [S.FixedItem(0, 2 * DAY, "event")]
    result = solve([task("a", 1, earliest=0)], fixed=fixed)
    assert result.starts["a"] is None


def test_far_deadline_waits_when_only_poor_slots_are_free():
    horizon = 2 * DAY
    fixed = [S.FixedItem(at(0, 8), at(0, 23), "event"), S.FixedItem(at(1, 8), at(1, 23), "event")]
    tasks = [task("a", 1, due=horizon + 30 * DAY, deferrable=True, defer_utility=-1.6)]
    assert solve(tasks, fixed=fixed).starts["a"] is None


def test_far_deadline_pulled_in_when_later_weeks_are_full():
    horizon = 2 * DAY
    fixed = [S.FixedItem(at(0, 8), at(0, 23), "event"), S.FixedItem(at(1, 8), at(1, 23), "event")]
    tasks = [task("a", 1, due=horizon + 5 * DAY, deferrable=True, defer_utility=-1.6)]
    capacity = [S.CapacityBound(due_by=horizon + 7 * DAY, capacity_min=10)]
    assert solve(tasks, fixed=fixed, capacity=capacity).starts["a"] is not None


def test_far_deadline_placed_now_when_a_good_slot_is_free():
    tasks = [task("a", 1, due=2 * DAY + 30 * DAY, deferrable=True, defer_utility=-1.6)]
    start = solve(tasks).starts["a"]
    assert start is not None and at(0, 10) <= start


def test_result_never_worse_than_the_warm_start():
    fixed = [S.FixedItem(at(0, 13), at(0, 14), "event", label="Uni")]
    tasks = [
        task("a", 1, current=at(0, 20), hint=at(0, 20), stability=1.0, label="Uni"),
        task("b", 2, current=at(1, 9), hint=at(1, 9), stability=1.0),
        task("c", 0.5),
    ]
    result = solve(tasks, fixed=fixed)
    assert result.hint_objective is not None
    assert result.objective <= result.hint_objective + 1e-9


def test_neighbour_weights_pull_same_label_together():
    weights = dict(S.F.prior_mean())
    weights["nb_prev:same_label"] = 3.0
    fixed = [S.FixedItem(at(0, 15), at(0, 16), "event", label="Uni")]
    tasks = [task("a", 1, label="Uni", due=at(0, 23))]
    start = solve(tasks, fixed=fixed, weights=weights).starts["a"]
    assert at(0, 16) <= start <= at(0, 17)


def test_candidate_mode_still_places_everything():
    tasks = [task(f"t{n}", 1) for n in range(6)]
    result = solve(tasks, neighbour_mode="candidate")
    assert result.neighbour_mode == "candidate"
    assert all(start is not None for start in result.starts.values())
