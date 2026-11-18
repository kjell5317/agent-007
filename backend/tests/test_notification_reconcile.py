"""State notifications: which should show, the diff against the ledger, and
sending only once scheduling has settled."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.commute.legs import PlannedLeg, RoutingIssues
from app.services.notifications import reconcile, settle

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 17, 12, tzinfo=TZ)


def state(**kw) -> reconcile.TaskState:
    base = dict(
        id=uuid.uuid4(), title="Tax form", due=NOW + timedelta(days=2), deadline_kind="hard",
        schedule_state="placed", slot=(NOW + timedelta(hours=3), NOW + timedelta(hours=4)),
    )
    base.update(kw)
    return reconcile.TaskState(**base)


def row(key: str, fingerprint: str, *, cleared: bool = False):
    return SimpleNamespace(key=key, fingerprint=fingerprint, tag=f"tag-{key}",
                           cleared_at=NOW if cleared else None)


@pytest.mark.parametrize(
    ("task", "case"),
    [
        (state(), None),
        (state(slot=(NOW + timedelta(days=2), NOW + timedelta(days=2, hours=1))), "late"),
        (state(schedule_state="unplaced", slot=None), "unplaced"),
        (state(schedule_state="deferred", slot=None, due=NOW + timedelta(days=6)), "later"),
        (state(schedule_state="deferred", slot=None, due=NOW + timedelta(days=9)), None),
        (state(due=NOW - timedelta(hours=1)), "passed"),
        # Waiting on someone: no warning until it's overdue.
        (state(waiting=True, schedule_state="waiting", slot=None), None),
        (state(waiting=True, schedule_state="waiting", slot=None, due=NOW - timedelta(hours=1)), "passed"),
    ],
)
def test_deadline_case(task, case):
    assert reconcile.deadline_case(task, NOW) == case


def test_only_a_fresh_passed_deadline_is_news():
    fresh, stale = reconcile.past_deadline([
        state(due=NOW - timedelta(hours=2)), state(due=NOW - timedelta(days=3)),
    ], NOW)
    assert fresh.news and not stale.news
    # A stale one already showing stays; one never sent isn't sent now.
    plan = reconcile.plan([fresh, stale], [row(stale.key, stale.fingerprint)])
    assert [d.key for d in plan.send] == [fresh.key] and plan.clear == []
    assert reconcile.plan([stale], []).send == []


def test_a_running_task_keeps_its_warning_without_a_new_one():
    task = state(running=True, due=NOW - timedelta(minutes=30))
    [item] = reconcile.past_deadline([task], NOW)
    assert not item.news
    # Showing: it stays (not cleared, so the next pause doesn't re-send it).
    assert reconcile.plan([item], [row(item.key, item.fingerprint)]) == reconcile.Plan([], [])
    # Not showing: nothing goes out while the work is under way.
    assert reconcile.plan([item], []).send == []


def test_plan_sends_new_resends_changed_and_clears_resolved():
    late = reconcile.past_deadline(
        [state(slot=(NOW + timedelta(days=2), NOW + timedelta(days=2, hours=1)))], NOW
    )[0]
    showing = row(late.key, late.fingerprint)
    resolved = row("deadline:resolved", "x")

    assert reconcile.plan([late], []).send == [late]
    # Already showing with the same content: nothing to do, however often
    # the reconciler runs — this is what stops the old send/revoke flicker.
    assert reconcile.plan([late], [showing]).send == []
    # The deadline moved: the push is updated.
    assert reconcile.plan([late], [row(late.key, "other")]).send == [late]
    # Cleared once, late again after a real resolution: news again.
    assert reconcile.plan([late], [row(late.key, late.fingerprint, cleared=True)]).send == [late]
    # No longer wanted: cleared.
    assert reconcile.plan([late], [showing, resolved]).clear == [resolved]


def _leg(origin_anchor, dest_anchor, origin, destination, hour):
    return PlannedLeg(
        origin_anchor=origin_anchor, dest_anchor=dest_anchor, origin=origin, destination=destination,
        mode="failed", depart=NOW.replace(hour=hour), arrive=NOW.replace(hour=hour, minute=30),
    )


def test_no_route_goes_out_once_per_event_not_again_for_the_trip_back():
    there = _leg("home", "ev-1", "Home", "Unknown Str. 1", 9)
    back = _leg("ev-1", "home", "Unknown Str. 1", "Home", 17)
    desired = reconcile.commute(RoutingIssues(unroutable=[back, there], conflicts=[]))

    assert len(desired) == 1
    [item] = desired
    assert item.key == "noroute:ev-1" and item.message.body.startswith("Unknown Str. 1")
    # Once sent, it never goes out again for that address — even after it
    # cleared and the legs got rewritten.
    assert reconcile.plan(desired, [row(item.key, item.fingerprint, cleared=True)]).send == []
    # A corrected address that fails again is new.
    assert reconcile.plan(desired, [row(item.key, "old address", cleared=True)]).send == desired


def test_a_trip_between_two_events_blames_neither_address():
    gym_there = _leg("home", "ev-gym", "Home", "Unknown Str. 1", 9)
    gym_to_dinner = _leg("ev-gym", "ev-dinner", "Unknown Str. 1", "Main St. 5", 12)
    office_to_dinner = _leg("ev-office", "ev-dinner", "Office Rd. 2", "Main St. 5", 17)
    desired = reconcile.commute(RoutingIssues(
        unroutable=[gym_there, gym_to_dinner, office_to_dinner], conflicts=[],
    ))
    # The gym's bad address explains its onward trip; the other trip names
    # both ends instead of pinning it on dinner.
    assert [d.key for d in desired] == ["noroute:ev-gym", "noroute:ev-office:ev-dinner"]
    assert desired[1].message.body.startswith("Office Rd. 2 → Main St. 5")


def test_conflict_is_keyed_by_leg_and_blocker():
    leg = _leg("home", "ev-1", "Home", "Gym", 18)
    [item] = reconcile.commute(RoutingIssues(unroutable=[], conflicts=[(leg, "ev-dinner", "Dinner")]))
    assert item.key == "conflict:home:ev-1" and item.fingerprint == "ev-dinner"
    assert "Dinner" in item.message.body


@pytest.mark.asyncio
async def test_settle_reconciles_once_after_a_burst_of_solves(monkeypatch):
    runs = []

    async def fake_reconcile():
        runs.append(settle._routing)

    monkeypatch.setattr(settle, "SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(settle, "reconcile_now", fake_reconcile)
    monkeypatch.setattr(settle, "_routing", None)
    issues = RoutingIssues(unroutable=[], conflicts=[])

    settle.solve_finished(None)
    settle.solve_finished(issues)
    settle.solve_finished(None)  # wrote no legs: the last known issues stand
    await asyncio.sleep(0.05)

    assert runs == [issues]


@pytest.mark.asyncio
async def test_reconcile_waits_while_a_solve_runs(monkeypatch):
    monkeypatch.setattr("app.services.plan.replan.busy", lambda: True)
    assert await settle.reconcile_now() is None


@pytest.mark.asyncio
async def test_reconcile_session_sends_clears_and_records(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(), title="Tax form", due_date=NOW - timedelta(hours=1), deadline_kind="hard",
        schedule_state="placed", scheduled_date=NOW + timedelta(hours=1), booked_minutes=60,
        estimation=60, running_since=None,
    )
    stale = row("deadline:gone", "x")
    ledger_rows = [stale]
    sent, cleared, recorded = [], [], []

    async def fake_send(message):
        sent.append(message)

    async def fake_clear(tag):
        cleared.append(tag)

    monkeypatch.setattr(settle.tasks_store, "open_schedulable", lambda _s: [task])
    monkeypatch.setattr(settle.ledger, "of_kinds", lambda _s, kinds: ledger_rows)
    monkeypatch.setattr(settle.ledger, "record_sent", lambda _s, **kw: recorded.append(kw["key"]))
    monkeypatch.setattr(settle.ledger, "record_cleared", lambda _s, r, _now: setattr(r, "cleared_at", NOW))
    monkeypatch.setattr(settle.push, "send", fake_send)
    monkeypatch.setattr(settle.push, "clear", fake_clear)

    result = await settle.reconcile_session(SimpleNamespace(commit=lambda: None), routing=None)

    assert result == {"sent": 1, "cleared": 1}
    assert recorded == [f"deadline:{task.id}"]
    assert sent[0].title.startswith("⚠️ Deadline passed")
    assert cleared == ["tag-deadline:gone"]
