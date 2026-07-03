from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.db.models.route_cache import RouteCache  # noqa: E402
from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.calendar.discover import _physical_span  # noqa: E402
from app.services.commute.legs import FAILED_MODE, Anchor, PlannedLeg  # noqa: E402
from app.services.commute.planner import (  # noqa: E402
    _description_for,
    _navigation_url,
    _reschedule_candidates,
)
from app.services.commute.reschedule import _first_overlap  # noqa: E402
from app.services.plan import schedule as schedule_service  # noqa: E402
from app.services.plan.schedule import (  # noqa: E402
    BusyEvent,
    Interval,
    _block_total,
    _chain_insert_slot,
    _cached_trip_legs,
    _effective_freed_range,
    _piggyback_slot,
    _planned_from_block,
)
from app.timezones import user_tz  # noqa: E402

GYM = "Gymstreet 5, Munich"
OFFICE = "Officeplatz 2, Munich"
LIBRARY = "Bookweg 3, Munich"

BUFFER = timedelta(minutes=15)


def _route_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    RouteCache.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _day_at(hour: int, minute: int = 0, day_offset: int = 3) -> datetime:
    base = datetime.now(user_tz()) + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _url_query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_transit_navigation_url_includes_departure_time():
    depart = datetime(2026, 7, 3, 8, 30, tzinfo=timezone(timedelta(hours=2)))
    arrive = depart + timedelta(minutes=40)
    leg = PlannedLeg(
        origin_anchor="home",
        dest_anchor="office",
        origin="Home",
        destination=OFFICE,
        mode="transit",
        depart=depart,
        arrive=arrive,
    )

    query = _url_query(_navigation_url(leg))

    assert query["travelmode"] == ["transit"]
    assert query["departure_time"] == [
        str(int(depart.astimezone(timezone.utc).timestamp()))
    ]

    navigate_line = next(
        line.removeprefix("Navigate: ")
        for line in _description_for(leg).splitlines()
        if line.startswith("Navigate: ")
    )
    assert _url_query(navigate_line)["departure_time"] == query["departure_time"]


@pytest.mark.parametrize("mode", ["bicycling", FAILED_MODE])
def test_non_transit_navigation_url_omits_departure_time(mode):
    depart = _day_at(8)
    leg = PlannedLeg(
        origin_anchor="home",
        dest_anchor="office",
        origin="Home",
        destination=OFFICE,
        mode=mode,
        depart=depart,
        arrive=depart + timedelta(minutes=30),
    )

    query = _url_query(_navigation_url(leg))

    assert "departure_time" not in query
    if mode == FAILED_MODE:
        assert "travelmode" not in query
    else:
        assert query["travelmode"] == [mode]


def test_block_geometry_wraps_task_with_legs():
    duration = timedelta(minutes=30)
    total = _block_total(duration, BUFFER, 600, 900)
    assert total == timedelta(seconds=600) + BUFFER + duration + BUFFER + timedelta(seconds=900)

    block_start = _day_at(12)
    planned = _planned_from_block((block_start, block_start + total), duration, BUFFER, 600, 900)
    assert planned.start == block_start + timedelta(seconds=600) + BUFFER
    assert planned.end == planned.start + duration
    assert planned.block_end == planned.end + BUFFER + timedelta(seconds=900)


def test_block_geometry_without_legs_is_bare_task():
    duration = timedelta(minutes=30)
    assert _block_total(duration, BUFFER, 0, 0) == duration
    block_start = _day_at(12)
    planned = _planned_from_block((block_start, block_start + duration), duration, BUFFER, 0, 0)
    assert planned.start == block_start
    assert planned.block_end == planned.end


def test_cached_trip_legs_reuse_one_bike_direction_for_round_trip():
    session = _route_session()
    session.add(
        RouteCache(
            origin="Homestreet 1",
            destination=GYM,
            mode="bicycling",
            hour_bucket=0,
            duration_seconds=600,
        )
    )
    session.commit()
    task = SimpleNamespace(location=GYM)
    settings = SimpleNamespace(
        commute_enabled=True,
        google_maps_api_key="key",
        home_address="Homestreet 1",
    )

    assert _cached_trip_legs(session, task, settings) == (600, 600)


def test_piggyback_after_anchor_ignores_its_replaced_leg():
    anchor_id = "gym-class"
    busy = [
        BusyEvent(anchor_id, _day_at(14), _day_at(15), "busy", location=GYM),
        # The anchor's current inbound leg sits right where the task wants to
        # go — it gets re-derived around the task, so it must not block.
        BusyEvent(
            "leg-home", _day_at(15, 15), _day_at(15, 45), "commute",
            leg_key=(anchor_id, "home"),
        ),
    ]
    planned = _piggyback_slot(
        busy,
        location=GYM.upper(),
        duration=timedelta(minutes=30),
        buffer=BUFFER,
        out_s=600,
        in_s=600,
        window_start=_day_at(8),
        window_end=_day_at(20, 0, day_offset=4),
    )

    assert planned is not None
    assert planned.start == _day_at(15, 15)
    assert planned.end == _day_at(15, 45)
    assert planned.out_s == 0  # arrives with the anchor's trip
    assert planned.in_s == 600
    assert planned.block_end == planned.end + BUFFER + timedelta(seconds=600)


def test_piggyback_respects_real_conflicts():
    anchor_id = "gym-class"
    busy = [
        BusyEvent(anchor_id, _day_at(14), _day_at(15), "busy", location=GYM),
        BusyEvent("other", _day_at(15, 20), _day_at(16), "busy"),
        BusyEvent("before", _day_at(12, 30), _day_at(14), "busy"),
    ]
    planned = _piggyback_slot(
        busy,
        location=GYM,
        duration=timedelta(minutes=30),
        buffer=BUFFER,
        out_s=600,
        in_s=600,
        window_start=_day_at(8),
        window_end=_day_at(20),
    )
    assert planned is None


@pytest.mark.asyncio
async def test_chain_insert_picks_min_added_travel(monkeypatch):
    travel = {
        (GYM, LIBRARY): 600,
        (LIBRARY, OFFICE): 600,
        (GYM, OFFICE): 300,
    }

    async def fake_one_way(_session, origin, destination, _reference):
        return travel.get((origin, destination))

    monkeypatch.setattr(schedule_service, "_one_way_seconds", fake_one_way)

    prev = BusyEvent("ev-gym", _day_at(10), _day_at(11), "busy", location=GYM)
    nxt = BusyEvent("ev-office", _day_at(14), _day_at(15), "busy", location=OFFICE)
    planned = await _chain_insert_slot(
        None,
        [prev, nxt],
        location=LIBRARY,
        duration=timedelta(minutes=30),
        buffer=BUFFER,
        window_start=_day_at(8),
        window_end=_day_at(20),
    )

    assert planned is not None
    # Packed late against the next anchor: leg + buffers walk back from it.
    assert planned.end == nxt.start - 2 * BUFFER - timedelta(seconds=600)
    assert planned.start == planned.end - timedelta(minutes=30)
    assert planned.out_s == 600 and planned.in_s == 600
    assert planned.block_start == planned.start - BUFFER - timedelta(seconds=600)
    assert planned.block_end == planned.end + BUFFER + timedelta(seconds=600)


@pytest.mark.asyncio
async def test_chain_insert_skips_unroutable_and_tight_gaps(monkeypatch):
    async def fake_one_way(_session, _origin, _destination, _reference):
        return None

    monkeypatch.setattr(schedule_service, "_one_way_seconds", fake_one_way)
    prev = BusyEvent("a", _day_at(10), _day_at(11), "busy", location=GYM)
    nxt = BusyEvent("b", _day_at(14), _day_at(15), "busy", location=OFFICE)
    planned = await _chain_insert_slot(
        None,
        [prev, nxt],
        location=LIBRARY,
        duration=timedelta(minutes=30),
        buffer=BUFFER,
        window_start=_day_at(8),
        window_end=_day_at(20),
    )
    assert planned is None


def test_freed_range_ignores_victims_own_legs():
    victim = BusyEvent("task-1", _day_at(12), _day_at(13), "task", location=GYM)
    busy = [
        victim,
        BusyEvent("leg-out", _day_at(11, 30), _day_at(11, 45), "commute", leg_key=("home", "task-1")),
        BusyEvent("leg-in", _day_at(13, 15), _day_at(13, 30), "commute", leg_key=("task-1", "home")),
        BusyEvent("meeting", _day_at(9), _day_at(10), "busy"),
        BusyEvent("dinner", _day_at(18), _day_at(19), "busy"),
    ]
    freed = _effective_freed_range(victim, busy, _day_at(8), _day_at(20))
    # The victim's own legs vacate with it — the hole spans meeting → dinner.
    assert freed.start == _day_at(10)
    assert freed.end == _day_at(18)


@pytest.mark.asyncio
async def test_plan_task_slot_reserves_whole_trip_block(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        title="Return library books",
        due_date=_day_at(20, 0, day_offset=2),
        scheduled_date=None,
        calendar_event_id=None,
        estimation=30,
        location=LIBRARY,
    )

    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(
            commute_event_buffer_minutes=15,
            google_calendar_default_event_minutes=30,
            google_calendar_id="",
            google_busy_calendar_ids=[],
            slot_min_lead_minutes=0,
            commute_enabled=True,
            google_maps_api_key="key",
            home_address="Homestreet 1",
        ),
    )

    async def fake_estimate(_session, _task, *, reference):
        return 600, 900, False

    monkeypatch.setattr(schedule_service, "_estimate_trip_legs", fake_estimate)
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda *a, **k: [])
    monkeypatch.setattr(schedule_service, "resolve_location_alias", lambda loc: loc)

    planned = await schedule_service.plan_task_slot(SimpleNamespace(), task)

    assert planned.end - planned.start == timedelta(minutes=30)
    assert planned.start - planned.block_start == timedelta(seconds=600) + BUFFER
    assert planned.block_end - planned.end == timedelta(seconds=900) + BUFFER
    # Packed against the day target, block inside the working window.
    assert planned.block_end.time() <= schedule_service.DAY_TARGET


@pytest.mark.asyncio
async def test_plan_task_slot_reserves_placeholders_for_unroutable(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        title="Visit mystery place",
        due_date=_day_at(20, 0, day_offset=2),
        scheduled_date=None,
        calendar_event_id=None,
        estimation=30,
        location="Nowhere 1",
    )

    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(
            commute_event_buffer_minutes=15,
            google_calendar_default_event_minutes=30,
            google_calendar_id="",
            google_busy_calendar_ids=[],
            slot_min_lead_minutes=0,
            commute_enabled=True,
            google_maps_api_key="key",
            home_address="Homestreet 1",
        ),
    )

    async def fake_one_way(_session, _origin, _destination, _reference):
        return None  # Maps can't route anything here

    monkeypatch.setattr(schedule_service, "_one_way_seconds", fake_one_way)
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda *a, **k: [])
    monkeypatch.setattr(schedule_service, "resolve_location_alias", lambda loc: loc)

    planned = await schedule_service.plan_task_slot(SimpleNamespace(), task)

    assert planned.unroutable is True
    # 30-minute failed placeholders reserved on both sides.
    assert planned.start - planned.block_start == timedelta(minutes=30) + BUFFER
    assert planned.block_end - planned.end == timedelta(minutes=30) + BUFFER


def test_colliding_task_anchor_is_replaced_not_overlapped():
    # Old-system task packed tight against a fixed event: its outbound leg
    # would land on top of the event → the task must be re-placed.
    meeting = Anchor("ev-meeting", _day_at(11), _day_at(12), OFFICE)
    task = Anchor("ev-task", _day_at(12, 5), _day_at(12, 35), GYM)
    leg = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-task", origin="Home", destination=GYM,
        mode="bicycling", depart=_day_at(11, 30), arrive=_day_at(11, 50),
    )
    candidates = _reschedule_candidates([leg], [meeting, task], {"ev-task"})
    assert candidates == {"ev-task"}

    # Same collision between two fixed events → nothing movable, no candidate.
    candidates = _reschedule_candidates([leg], [meeting, task], set())
    assert candidates == set()

    # No overlap → no candidate.
    clear_leg = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-task", origin="Home", destination=GYM,
        mode="bicycling", depart=_day_at(12, 0), arrive=_day_at(12, 5),
    )
    assert _reschedule_candidates([clear_leg], [meeting, task], {"ev-task"}) == set()


def _calendar_event(event_id, start, end, *, location=None, all_day=False, props=None):
    return CalendarEvent(
        id=event_id,
        calendar_id="primary",
        summary="Event",
        description=None,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        html_link=None,
        private_properties=props or {},
        raw={},
    )


def test_physical_span_covers_only_routable_events():
    commute_props = {"managed_by": "plan_service", "kind": "commute"}
    events = [
        _calendar_event("gym", _day_at(14), _day_at(15), location=GYM),
        _calendar_event("office", _day_at(9), _day_at(10), location=OFFICE),
        _calendar_event("call", _day_at(11), _day_at(12), location="https://zoom.us/j/1"),
        _calendar_event("leg", _day_at(13), _day_at(13, 30), location=GYM, props=commute_props),
        _calendar_event("holiday", _day_at(0), _day_at(23), location=GYM, all_day=True),
    ]
    span = _physical_span(events)
    assert span == (_day_at(9), _day_at(15))

    assert _physical_span([events[2], events[3], events[4]]) is None


def test_reschedule_overlap_skips_tasks_own_legs():
    slot = Interval(_day_at(12), _day_at(13), "ev-1")
    own = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-1", origin="Home", destination=GYM,
        mode="bicycling", depart=_day_at(12, 45), arrive=_day_at(13, 15),
    )
    foreign = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-2", origin="Home", destination=GYM,
        mode="bicycling", depart=_day_at(12, 45), arrive=_day_at(13, 15),
    )
    assert _first_overlap(slot, [own], "ev-1") is None
    hit = _first_overlap(slot, [foreign], "ev-1")
    assert hit is not None and hit.start == foreign.depart


@pytest.mark.asyncio
async def test_slot_keeps_buffer_before_event_starting_at_due(monkeypatch):
    tz = user_tz()
    due = (datetime.now(tz) + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    meeting = BusyEvent("meeting", due, due + timedelta(hours=1), "busy")
    wall = BusyEvent("wall", datetime.now(tz) - timedelta(hours=1), due - timedelta(hours=2), "busy")

    async def fake_fetch(session, time_min, time_max, **kwargs):
        # Google's timeMax is an exclusive bound on the event *start* — an
        # event starting exactly at time_max is not returned.
        return [ev for ev in (wall, meeting) if ev.start < time_max and ev.end > time_min]

    monkeypatch.setattr(schedule_service, "_fetch_busy_events", fake_fetch)
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda *a, **k: [])

    task = SimpleNamespace(
        id=uuid.uuid4(),
        due_date=due,
        location=None,
        estimation=60,
        calendar_event_id=None,
    )
    planned = await schedule_service.plan_task_slot(None, task)
    assert planned.end <= meeting.start - BUFFER
