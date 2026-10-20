"""`replan()` end to end with the calendar, the task table and the log faked."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db.models.task import Task
from app.services.plan import features as F
from app.services.plan import replan
from app.services.plan.timeline import Item

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)  # Monday morning


def make_task(title, *, due, scheduled=None, minutes=60, **kw) -> Task:
    task = Task(
        id=uuid.uuid4(), title=title, due_date=due, scheduled_date=scheduled, estimation=minutes,
        calendar_event_id=f"ev-{title}" if scheduled else None,
        schedule_state="placed" if scheduled else "unplaced",
        deadline_kind=kw.pop("deadline_kind", "firm"), placed_by=kw.pop("placed_by", "planner"),
        is_container=False, **kw,
    )
    return task


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(tasks=[], events=[], writes=[], deletes=[], logs=[], resolved={},
                            pushed=[])

    async def calendar_items(_session, _start, _end, account_key=None, blockers=False):
        return [e for e in state.events if blockers or e.kind != "blocker"]

    async def update_task_event(_session, task, *, start, end, changed_fields=None):
        state.writes.append((task.title, start, end))
        task.scheduled_date = start
        task.calendar_event_id = task.calendar_event_id or f"ev-{task.title}"

    async def delete_task_event(_session, task):
        state.deletes.append(task.title)
        task.calendar_event_id = None

    async def no_commutes(_session, _spans):
        return None

    monkeypatch.setattr(replan, "calendar_items", calendar_items)
    monkeypatch.setattr("app.services.calendar.update_task_event", update_task_event)
    monkeypatch.setattr("app.services.calendar.delete_task_event", delete_task_event)
    monkeypatch.setattr(replan, "_replan_commutes", no_commutes)
    monkeypatch.setattr(replan.tasks_store, "open_schedulable", lambda _s: state.tasks)
    monkeypatch.setattr(replan.tasks_store, "get", lambda _s, _id: None)
    monkeypatch.setattr(replan.log_store, "by_slot_for_tasks", lambda _s, _ids, _kinds: state.resolved)
    monkeypatch.setattr(replan.log_store, "add", lambda _s, **kw: state.logs.append(kw))
    monkeypatch.setattr("app.services.learn.jobs.weights_for", lambda _s, _d: F.prior_mean())
    monkeypatch.setattr("app.services.learn.jobs.rho", lambda _s: 0.5)
    monkeypatch.setattr("app.events.publish_task", lambda _s, _id: None)

    async def no_push(*_args, **_kw):
        state.pushed.append(_kw.get("title") or (_args[0] if _args else None))

    # Never reach a real phone from a test.
    monkeypatch.setattr("app.services.notify.notify", no_push)
    return state


def session():
    return SimpleNamespace(commit=lambda: None, flush=lambda: None, rollback=lambda: None)


@pytest.mark.asyncio
async def test_replan_places_moves_and_leaves_alone(world):
    lecture = Item(NOW.replace(hour=10), NOW.replace(hour=16), "event", label="Uni", event_id="e1")
    world.events = [lecture]
    fresh = make_task("fresh", due=NOW + timedelta(days=2))
    clash = make_task("clash", due=NOW + timedelta(days=3), scheduled=NOW.replace(hour=11))
    steady = make_task("steady", due=NOW + timedelta(days=5),
                       scheduled=(NOW + timedelta(days=1)).replace(hour=14))
    waiting = make_task("waiting", due=NOW + timedelta(days=1),
                        scheduled=NOW - timedelta(hours=1, minutes=30))
    world.tasks = [fresh, clash, steady, waiting]

    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)

    assert result.ok
    written = {title: (start, end) for title, start, end in world.writes}
    assert set(written) == {"fresh", "clash"}
    for start, end in written.values():
        assert end <= lecture.start or start >= lecture.end
        assert 10 <= start.hour and end.hour <= 19 or (end.hour == 19 and end.minute == 0)
    assert steady.title not in written  # stability: no gratuitous move
    assert waiting.id not in result.placed  # check-in outcome still open
    assert all(log["kind"] == "placed" and log["features"]["slot"] for log in world.logs)
    assert fresh.schedule_state == "placed"


@pytest.mark.asyncio
async def test_a_task_is_booked_exactly_as_long_as_it_is_set(world, monkeypatch):
    from app.services.learn import duration

    overruns = duration.fit([duration.Observation(60, 120, "-|-") for _ in range(30)])
    monkeypatch.setattr("app.services.learn.jobs.duration_params", lambda _s: overruns)
    task = make_task("sheet", due=NOW + timedelta(days=2), minutes=120, agent_estimation=90)
    world.tasks = [task]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    start, end = result.placed[task.id]
    assert end - start == timedelta(minutes=120)
    # The duration model keeps learning from the agent's raw estimate.
    assert [log["features"]["meta"]["estimate"] for log in world.logs] == [90]


@pytest.mark.asyncio
async def test_running_task_blocks_its_extension(world):
    running = make_task("running", due=NOW + timedelta(days=1),
                        scheduled=NOW - timedelta(minutes=90), running_since=NOW - timedelta(minutes=10))
    # Booked 60 min ending 08:30; at 09:00 the extension runs to 09:15.
    blocked = make_task("next", due=NOW + timedelta(hours=8), scheduled=NOW.replace(hour=9, minute=15))
    world.tasks = [running, blocked]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    assert "running" not in {title for title, _, _ in world.writes}
    start, _ = result.placed[blocked.id]
    assert start >= NOW.replace(hour=9, minute=15)


@pytest.mark.asyncio
async def test_running_extension_moves_slots_that_already_began(world):
    running = make_task("running", due=NOW + timedelta(days=1),
                        scheduled=NOW - timedelta(minutes=90), running_since=NOW - timedelta(minutes=10))
    # The extension runs 08:30–09:15: one slot inside it is over, one is "in progress".
    elapsed = make_task("elapsed", due=NOW + timedelta(hours=8), minutes=15,
                        scheduled=NOW.replace(hour=8, minute=30))
    begun = make_task("begun", due=NOW + timedelta(hours=8), scheduled=NOW.replace(hour=8, minute=45))
    world.tasks = [running, elapsed, begun]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    for task in (elapsed, begun):
        start, _ = result.placed[task.id]
        assert start >= NOW.replace(hour=9, minute=15)


@pytest.mark.asyncio
async def test_far_deadline_is_deferred_when_the_week_is_full(world):
    world.events = [Item(NOW.replace(hour=0), NOW.replace(hour=0) + timedelta(days=8), "event")]
    far = make_task("far", due=NOW + timedelta(days=60), scheduled=None)
    world.tasks = [far]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    assert far.id in result.deferred
    assert far.schedule_state == "deferred"
    assert [log["kind"] for log in world.logs] == ["deferred"]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(world):
    world.tasks = [make_task("fresh", due=NOW + timedelta(days=2))]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5, dry_run=True)
    assert result.ok and result.placed
    assert world.writes == [] and world.logs == []


@pytest.fixture
def routed(world, monkeypatch):
    """`world` plus commute inside the solver, with Maps, weather and the
    leg writer faked."""
    from app.config import get_settings
    from app.services.commute import planner
    from app.services.commute.state import CommuteState
    from app.services.plan import routing

    settings = get_settings().model_copy(update={
        "home_address": "Home", "google_calendar_id": "primary", "google_maps_api_key": "k",
    })
    monkeypatch.setattr(routing, "get_settings", lambda: settings)
    monkeypatch.setattr(routing, "routing_enabled", lambda: True)

    async def load_state(_session, _now, _end):
        return world.commute_state, []

    async def routable(location):
        return location

    minutes = {frozenset({"Home", "Uni"}): (15, 25), frozenset({"Home", "Library"}): (20, 30),
               frozenset({"Uni", "Library"}): (10, 20)}

    async def travel(locations, _grid):
        out = {}
        for a, x in enumerate(locations):
            for b, y in enumerate(locations):
                if a != b:
                    bike, transit = minutes[frozenset({x, y})]
                    # Odd seconds, as Maps returns them.
                    out[(a, b, "bicycling")] = bike * 60 + 20
                    out[(a, b, "transit")] = transit * 60 + 40
        return out

    async def no_weather(*_args):
        return {}

    async def write_legs(_session, legs, **kw):
        world.legs = legs
        return len(legs), [], []

    async def nothing(*_args, **_kw):
        return None

    world.commute_state = CommuteState()
    world.legs = []
    monkeypatch.setattr("app.services.commute.state.load_state", load_state)
    monkeypatch.setattr("app.services.location.resolve_routable_location", routable)
    monkeypatch.setattr(routing, "_travel_matrix", travel)
    monkeypatch.setattr(routing, "_weather", no_weather)
    monkeypatch.setattr("app.db.clients.commute_log.active_pins", lambda _s, _now: {})
    monkeypatch.setattr(planner, "_write_legs", write_legs)
    monkeypatch.setattr(planner, "_notify_new_failures", nothing)
    monkeypatch.setattr(planner, "_notify_new_conflicts", nothing)
    monkeypatch.setattr(planner, "_sync_anchor_reminders", nothing)
    return world


@pytest.mark.asyncio
async def test_replan_writes_the_solved_route(routed):
    lecture = Item(NOW.replace(hour=10), NOW.replace(hour=12), "event", event_id="lec", location="Uni")
    routed.events = [lecture]
    library = make_task("library", due=NOW.replace(hour=22), location="Library")
    routed.tasks = [library]

    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)

    assert result.ok and result.routes
    keys = [(leg.origin_anchor, leg.dest_anchor) for leg in routed.legs]
    # The new task's provisional key was replaced by its new calendar event.
    assert library.calendar_event_id == "ev-library"
    assert any("ev-library" in key for key in keys)
    assert not any(key[0].startswith("task:") or key[1].startswith("task:") for key in keys)
    assert keys[0][0] == "home" and keys[-1][1] == "home"
    start, end = result.placed[library.id]
    assert all(leg.arrive <= start or leg.depart >= end for leg in routed.legs)
    assert all(leg.mode in ("bicycling", "transit") for leg in routed.legs)
    for leg in routed.legs:
        assert leg.depart.minute % 5 == 0 and leg.depart.second == 0
        assert (leg.arrive - leg.depart) % timedelta(minutes=5) == timedelta(0)


@pytest.mark.asyncio
async def test_replan_starts_where_the_bike_was_left(routed):
    from app.services.commute.state import CommuteState

    routed.commute_state = CommuteState(bike_location="Uni")
    lecture = Item(NOW.replace(hour=10), NOW.replace(hour=12), "event", event_id="lec", location="Uni")
    routed.events = [lecture]
    await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    out = next(leg for leg in routed.legs if leg.dest_anchor == "lec")
    back = next(leg for leg in routed.legs if leg.origin_anchor == "lec")
    assert out.mode == "transit" and back.mode == "bicycling"
    assert back.note and "riding it back" in back.note


@pytest.mark.asyncio
async def test_location_blocker_routes_without_claiming_the_time(routed):
    # Show-as-free "working at the library" block over the whole working day.
    bib = Item(NOW.replace(hour=10), NOW.replace(hour=19), "blocker", event_id="bib",
               location="Library")
    routed.events = [bib]
    homework = make_task("homework", due=NOW.replace(hour=22))
    lecture_prep = make_task("prep", due=NOW.replace(hour=22), location="Uni")
    routed.tasks = [homework, lecture_prep]

    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)

    assert result.ok
    keys = {(leg.origin_anchor, leg.dest_anchor) for leg in routed.legs}
    assert any(key[1] == "bib" for key in keys) and any(key[0] == "bib" for key in keys)
    start, end = result.placed[homework.id]
    assert bib.start <= start and end <= bib.end
    start, end = result.placed[lecture_prep.id]
    assert end <= bib.start or start >= bib.end


@pytest.mark.asyncio
async def test_released_hand_placed_task_only_yields_when_in_the_way(world):
    # 06:00 is a poor hour under the prior; the user chose it anyway.
    early = (NOW + timedelta(days=1)).replace(hour=6)
    mine = make_task("mine", due=NOW + timedelta(days=3), scheduled=early, placed_by="user",
                     placed_at=NOW - timedelta(hours=1), created_at=NOW - timedelta(days=2))
    world.tasks = [mine]
    await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    assert world.writes == [] and mine.placed_context["tasks"]

    # Something new arrives (a repeat instance counts) and fits elsewhere:
    # the hand-placed task doesn't move for the model's own preferences.
    world.tasks.append(make_task("new", due=NOW + timedelta(days=2), created_at=NOW,
                                 recurrence_series_id=uuid.uuid4()))
    await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    assert "mine" not in {title for title, _, _ in world.writes}


@pytest.mark.asyncio
async def test_released_hand_placed_task_gives_way_to_a_deadline(world, monkeypatch):
    # Today is booked except 14:00–15:00, which the user filled by hand.
    world.events = [
        Item(NOW.replace(minute=15), NOW.replace(hour=14), "event", event_id="e1"),
        Item(NOW.replace(hour=15), NOW.replace(hour=23, minute=59), "event", event_id="e2"),
    ]
    mine = make_task("mine", due=NOW + timedelta(days=3), scheduled=NOW.replace(hour=14),
                     placed_by="user", placed_at=NOW - timedelta(hours=1),
                     created_at=NOW - timedelta(days=2))
    urgent = make_task("urgent", due=NOW.replace(hour=23), created_at=NOW, deadline_kind="firm")
    world.tasks = [mine, urgent]
    await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    written = {title: start for title, start, _ in world.writes}
    assert written["urgent"] == NOW.replace(hour=14)
    assert "mine" in written


@pytest.mark.asyncio
async def test_late_placement_warns(world, monkeypatch):
    warned = []

    async def deadline_missed(task, *, start, end):
        warned.append(task.title)

    monkeypatch.setattr("app.services.notify.notify_deadline_missed", deadline_missed)
    # Booked solid until tomorrow evening; the task is due tomorrow noon.
    world.events = [Item(NOW.replace(minute=15), (NOW + timedelta(days=1)).replace(hour=20),
                         "event", event_id="e1")]
    world.tasks = [make_task("squeezed", due=(NOW + timedelta(days=1)).replace(hour=12))]
    await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    assert warned == ["squeezed"]


@pytest.mark.asyncio
async def test_replan_is_idempotent(world):
    """A solve right after a solve changes nothing: the solver's own writes
    don't feed back into it."""
    world.events = [Item(NOW.replace(hour=10), NOW.replace(hour=16), "event", event_id="e1")]
    world.tasks = [
        make_task("a", due=NOW + timedelta(days=1)),
        make_task("b", due=NOW + timedelta(days=2), minutes=90),
        make_task("c", due=NOW + timedelta(days=4), scheduled=(NOW + timedelta(days=1)).replace(hour=11)),
    ]
    await replan.replan(session(), reason="first", now=NOW, time_limit=5)
    for task in world.tasks:
        task.schedule_state = "placed"
    world.writes.clear()
    await replan.replan(session(), reason="second", now=NOW, time_limit=5)
    assert world.writes == []
