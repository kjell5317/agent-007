"""Slot features, the prior, and the timeline helpers that feed them."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services.plan import features as F
from app.services.plan import timeline as T

TZ = ZoneInfo("Europe/Berlin")


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def ctx(**kw) -> F.SlotContext:
    base = dict(
        start=dt(20, 14), end=dt(20, 15), now=dt(19, 9), due=dt(23, 18),
        deadline_kind="firm", label="Uni", work_type="cognitive", effort="high",
    )
    base.update(kw)
    return F.SlotContext(**base)


def test_features_of_a_plain_afternoon_slot():
    feats = F.features(ctx())
    assert feats["h_start:14"] == 1 and feats["h_end:14"] == 1
    assert feats["h_wt:14:cognitive"] == 1 and feats["h_eff:14:high"] == 1
    assert feats["wd:1"] == 1 and feats["wd_wt:1:cognitive"] == 1
    assert feats["days_from_now"] == 1
    assert feats["gap_prev:15+"] == 1 and feats["nb_next:none"] == 1
    assert feats["htd:72+"] == 1
    assert feats["load:<240"] == 1
    assert not any(name.startswith("late") for name in feats)
    assert set(feats) <= set(F.feature_names())


def test_neighbour_and_gap_features():
    prev = F.Neighbour(kind="event", gap_min=5, label="Uni")
    nxt = F.Neighbour(kind="task", gap_min=40, label="Sport", work_type="physical", effort="low")
    feats = F.features(ctx(prev=prev, next=nxt))
    assert feats["gap_prev:5"] == 1 and feats["nb_prev:event"] == 1
    assert feats["nb_prev:same_label"] == 1
    assert feats["gap_next:15+"] == 1 and feats["nb_next:task"] == 1
    assert feats["nb_next:wt:cognitive:physical"] == 1
    assert feats["nb_next:eff:high:low"] == 1
    far = F.features(ctx(next=F.Neighbour(kind="event", gap_min=90)))
    assert far["nb_next:none"] == 1


def test_lateness_per_deadline_kind():
    feats = F.features(ctx(start=dt(23, 19), end=dt(23, 21), deadline_kind="hard"))
    assert feats["late:hard"] == 1 and feats["late_h:hard"] == 3
    # A late slot also falls in the closest hours-to-deadline bucket.
    assert feats["htd:0-6"] == 1


def test_load_buckets():
    assert F.features(ctx(day_load_min=400))["load:360-480"] == 1
    assert F.features(ctx(day_load_min=500))["load:>480"] == 1


def test_prior_reproduces_the_old_day_window():
    prior = F.prior_mean()
    inside = F.utility(F.features(ctx(start=dt(20, 11), end=dt(20, 13))), prior)
    evening = F.utility(F.features(ctx(start=dt(20, 19), end=dt(20, 21))), prior)
    night = F.utility(F.features(ctx(start=dt(20, 2), end=dt(20, 3))), prior)
    assert inside > evening > night
    assert evening - inside == -2
    assert night - inside == -8


def test_every_emitted_feature_has_a_prior():
    names = set(F.feature_names())
    feats = F.features(ctx(
        prev=F.Neighbour(kind="task", gap_min=0, label="Uni", work_type="social", effort="medium"),
        next=F.Neighbour(kind="event", gap_min=10, label="Uni"),
        start=dt(23, 23), end=dt(24, 1),
    ))
    assert set(feats) <= names
    assert set(F.prior_mean()) == names


def test_grid_round_trip_across_dst():
    grid = T.Grid(date(2026, 10, 24), 3, TZ)  # DST ends on Oct 25
    slot = grid.floor(dt(26, 14, 7))
    assert slot % 288 == 14 * 12 + 1
    assert grid.at(slot) == dt(26, 14, 5)
    assert grid.ceil(dt(26, 14, 7)) == slot + 1


def test_neighbours_and_day_load():
    task_id = uuid.uuid4()
    items = [
        T.Item(dt(20, 9), dt(20, 10), "event", label="Uni", event_id="e1"),
        T.Item(dt(20, 12), dt(20, 13), "event", label="Uni", event_id="e2"),
        T.Item(dt(20, 15, 10), dt(20, 16), "task", label="Sport", task_id=uuid.uuid4()),
        T.Item(dt(20, 14), dt(20, 15), "task", task_id=task_id),
    ]
    prev, nxt = T.neighbours(items, dt(20, 14), dt(20, 15), exclude=str(task_id))
    assert prev.label == "Uni" and prev.gap_min == 60
    assert nxt.kind == "task" and nxt.gap_min == 10
    assert T.day_load(items, date(2026, 10, 20), TZ, exclude=str(task_id)) == 60 + 60 + 50


def test_hand_placed_lock_holds_until_something_changes():
    placed = dt(19, 9)
    slot = (dt(20, 15), dt(20, 16))
    lecture = T.Item(dt(20, 10), dt(20, 12), "event", event_id="e1")
    tasks = [("a", dt(22, 18), 60), ("b", dt(23, 18), 30)]
    stored = T.lock_snapshot(slot, placed, tasks, [lecture])

    def holds(current_tasks=tasks, events=(lecture,), created=(), events_created=()):
        current = T.lock_snapshot(slot, placed, list(current_tasks), list(events))
        return T.lock_holds(stored, current, tasks_created=list(created),
                            events_created=list(events_created))

    assert holds()
    # Anything issued after the placement releases it — a repeat instance too …
    assert not holds(created=[("c", dt(19, 12))])
    # … but not an old task the snapshot simply didn't cover.
    assert holds(created=[("old", dt(18, 9))])
    assert not holds(events_created=[("e2", dt(19, 10))])
    # Deadline or duration changes release it; an event moving doesn't.
    assert not holds(current_tasks=[("a", dt(22, 20), 60), tasks[1]])
    assert not holds(current_tasks=[("a", dt(22, 18), 90), tasks[1]])
    longer = T.Item(dt(20, 10), dt(20, 13), "event", event_id="e1")
    assert not holds(events=(longer,))
    moved = T.Item(dt(20, 11), dt(20, 13), "event", event_id="e1")
    assert holds(events=(moved,))


def test_free_good_minutes_subtracts_busy_time():
    hours = T.good_hours(F.prior_mean())
    assert hours == set(range(10, 19))
    items = [T.Item(dt(20, 9), dt(20, 11), "event")]
    free = T.free_good_minutes(items, date(2026, 10, 20), date(2026, 10, 20), hours, TZ)
    assert free[date(2026, 10, 20)] == 9 * 60 - 60
