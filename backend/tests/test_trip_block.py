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
from app.services.calendar.discover import _replan_span  # noqa: E402
from app.services.commute import planner as commute_planner  # noqa: E402
from app.services.commute.legs import FAILED_MODE, Anchor, PlannedLeg  # noqa: E402
from app.services.commute.planner import (  # noqa: E402
    _description_for,
    _partition,
    _navigation_url,
    _resolve_routable_anchors,
    _reschedule_candidates,
)
from app.services.commute.reschedule import _first_overlap  # noqa: E402
from app.services.plan import schedule as schedule_service  # noqa: E402
from app.services.plan.schedule import (  # noqa: E402
    BusyEvent,
    Gaps,
    Interval,
    PlannedSlot,
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

BUFFER = timedelta(minutes=5)
EVENT_BUFFER = timedelta(minutes=15)
GAPS = Gaps(commute=BUFFER, event=EVENT_BUFFER)


def _route_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    RouteCache.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _day_at(hour: int, minute: int = 0, day_offset: int = 3) -> datetime:
    base = datetime.now(user_tz()) + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _url_query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def test_leg_event_format():
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
        reason="rain 80% >= 30% threshold",
    )

    from app.services.commute.planner import _summary_for

    # Title is just the mode emoji — destination lives in the location field.
    assert _summary_for(leg) == "🚆"
    assert _summary_for(replace_leg(leg, mode="bicycling")) == "🚲"
    assert _summary_for(replace_leg(leg, mode=FAILED_MODE)) == "⚠️ No route"

    # The Maps deep link ignores departure_time — never send it.
    query = _url_query(_navigation_url(leg))
    assert query["travelmode"] == ["transit"]
    assert "departure_time" not in query

    desc = _description_for(leg)
    lines = desc.splitlines()
    assert lines[0] == "From: Home"
    assert not any(line.startswith(("To:", "Mode:")) for line in lines)
    assert any(line.startswith("Reason: ") for line in lines)
    assert any(line.startswith("Updated: ") for line in lines)


def replace_leg(leg, **changes):
    from dataclasses import replace as dc_replace

    return dc_replace(leg, **changes)


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
        gaps=GAPS,
        out_s=600,
        in_s=600,
        window_start=_day_at(8),
        window_end=_day_at(20, 0, day_offset=4),
    )

    assert planned is not None
    # Task↔anchor is an event↔event boundary — the wider gap applies.
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
        gaps=GAPS,
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
        gaps=GAPS,
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
        gaps=GAPS,
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
            commute_event_buffer_minutes=5,
            event_buffer_minutes=15,
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
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy)
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
            commute_event_buffer_minutes=5,
            event_buffer_minutes=15,
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
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy)
    monkeypatch.setattr(schedule_service, "resolve_location_alias", lambda loc: loc)

    planned = await schedule_service.plan_task_slot(SimpleNamespace(), task)

    assert planned.unroutable is True
    # 30-minute failed placeholders reserved on both sides.
    assert planned.start - planned.block_start == timedelta(minutes=30) + BUFFER
    assert planned.block_end - planned.end == timedelta(minutes=30) + BUFFER


@pytest.mark.asyncio
async def test_estimate_trip_legs_routes_google_maps_task_location(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        location="https://www.google.com/maps/place/Library/@48.137154,11.576124,17z",
    )
    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(
            commute_enabled=True,
            google_maps_api_key="key",
            home_address="Homestreet 1",
        ),
    )
    calls = []

    async def fake_one_way(_session, origin, destination, reference):
        calls.append((origin, destination, reference))
        return 600

    monkeypatch.setattr(schedule_service, "_one_way_seconds", fake_one_way)

    reference = _day_at(12)
    out_s, in_s, unroutable = await schedule_service._estimate_trip_legs(
        SimpleNamespace(), task, reference=reference
    )

    assert (out_s, in_s, unroutable) == (600, 600, False)
    assert calls == [
        ("Homestreet 1", "48.137154,11.576124", reference),
        ("48.137154,11.576124", "Homestreet 1", reference),
    ]


@pytest.mark.asyncio
async def test_calendar_google_maps_anchor_routes_resolved_location(monkeypatch):
    event = _calendar_event(
        "event-1",
        _day_at(14),
        _day_at(15),
        location="https://www.google.com/maps/place/Library/@48.137154,11.576124,17z",
    )
    anchors, existing_commutes, online_spans = _partition([event], "primary")
    anchors = await _resolve_routable_anchors(anchors)
    calls = []

    async def fake_resolve_duration(_session, *, origin, destination, mode, departure):
        calls.append(
            {
                "origin": origin,
                "destination": destination,
                "mode": mode,
                "departure": departure,
            }
        )
        return 600

    monkeypatch.setattr(commute_planner, "resolve_duration", fake_resolve_duration)
    settings = SimpleNamespace(
        commute_bike_max_minutes=25,
        commute_rain_threshold_pct=30,
    )

    durations = await commute_planner._resolve_routes(
        SimpleNamespace(),
        anchors,
        "Homestreet 1",
        None,
        settings,
        {"errors": []},
    )

    assert online_spans == []
    assert existing_commutes == []
    assert anchors[0].location == "48.137154,11.576124"
    assert durations[("Homestreet 1", "48.137154,11.576124", "bicycling")] == 600
    assert durations[("48.137154,11.576124", "Homestreet 1", "bicycling")] == 600
    assert [(call["origin"], call["destination"], call["mode"]) for call in calls] == [
        ("Homestreet 1", "48.137154,11.576124", "bicycling"),
        ("48.137154,11.576124", "Homestreet 1", "bicycling"),
    ]


def test_legs_of_in_window_anchor_are_written_despite_sitting_outside():
    from app.services.commute.planner import _legs_to_write

    # A created/edited event replans exactly its own span — but its legs live
    # just outside that span (arrive ends before the event starts). They must
    # be written anyway.
    anchor = Anchor("ev1", _day_at(20, 30), _day_at(23), GYM)
    legs = [
        PlannedLeg(
            origin_anchor="home", dest_anchor="ev1", origin="Home", destination=GYM,
            mode="transit", depart=_day_at(19, 40), arrive=_day_at(20, 25),
        ),
        PlannedLeg(
            origin_anchor="ev1", dest_anchor="home", origin=GYM, destination="Home",
            mode="transit", depart=_day_at(23, 5), arrive=_day_at(23, 50),
        ),
    ]

    assert _legs_to_write(legs, [anchor], _day_at(20, 30), _day_at(23)) == legs
    # An anchor outside the window keeps the old span-overlap rule.
    far_anchor = Anchor("ev2", _day_at(9), _day_at(10), OFFICE)
    assert _legs_to_write(legs, [far_anchor], _day_at(20, 30), _day_at(23)) == []


def test_db_slot_overrides_stale_calendar_span(monkeypatch):
    # Mid-cascade, events.list can return a just-patched task at its old
    # position; the DB row written under the lock is the truth.
    row = SimpleNamespace(
        id=uuid.uuid4(),
        calendar_event_id="ev-9",
        scheduled_date=_day_at(15),
        estimation=60,
        location=None,
    )
    monkeypatch.setattr(
        "app.db.clients.tasks.open_scheduled_between",
        lambda session, *, time_min, time_max, exclude_task_id: [row],
    )
    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(
            commute_event_buffer_minutes=5,
            event_buffer_minutes=15,
            google_calendar_default_event_minutes=30,
            commute_enabled=False,
            google_maps_api_key="",
        ),
    )

    stale = BusyEvent("ev-9", _day_at(12), _day_at(13), "task")
    merged = schedule_service._db_scheduled_busy(
        None, SimpleNamespace(id=uuid.uuid4()), _day_at(8), _day_at(20), [stale],
    )

    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (_day_at(15), _day_at(16))
    assert merged[0].kind == "task"


@pytest.mark.asyncio
async def test_plan_window_never_reaches_beyond_lookahead(monkeypatch):
    # A task placed at `due - lead` can sit weeks out; its leg replan must
    # not write anything beyond now + lookahead — the daily re-baseline
    # derives the legs once the anchor slides into the window.
    monkeypatch.setattr(
        commute_planner,
        "get_settings",
        lambda: SimpleNamespace(
            google_calendar_id="primary",
            home_address="Homestreet 1",
            google_maps_api_key="key",
            commute_lookahead_days=7,
        ),
    )

    async def boom(*_args, **_kwargs):
        raise AssertionError("must not touch the calendar beyond the horizon")

    monkeypatch.setattr(commute_planner, "list_events_between", boom)

    start = datetime.now(timezone.utc) + timedelta(days=21)
    summary = await commute_planner.plan_window_commutes(
        None, window_start=start, window_end=start + timedelta(hours=5),
    )

    assert summary["planned"] == 0
    assert summary["errors"] == []


@pytest.mark.asyncio
async def test_stray_leg_cleanup_removes_past_and_beyond_horizon(monkeypatch):
    from app.services.commute.planner import delete_stray_commute_legs

    now = datetime.now(timezone.utc)
    commute_props = {"managed_by": "plan_service", "kind": "commute"}

    def leg(event_id, start, end):
        return _calendar_event(event_id, start, end, props=commute_props)

    past_leg = leg("past", now - timedelta(days=2), now - timedelta(days=2) + timedelta(minutes=30))
    live_leg = leg("live", now + timedelta(days=1), now + timedelta(days=1, minutes=30))
    stray_leg = leg("stray", now + timedelta(days=21), now + timedelta(days=21, minutes=30))
    far_task = _calendar_event("far-task", now + timedelta(days=21), now + timedelta(days=22))

    async def fake_list(session, *, calendar_ids, time_min, time_max, account_key=None):
        return [
            ev for ev in (past_leg, live_leg, stray_leg, far_task)
            if ev.start < time_max and ev.end > time_min
        ]

    deleted_ids = []

    async def fake_delete(session, *, calendar_id, event_id, account_key=None):
        deleted_ids.append(event_id)

    monkeypatch.setattr(commute_planner, "list_events_between", fake_list)
    monkeypatch.setattr(commute_planner, "delete_event", fake_delete)
    monkeypatch.setattr(
        commute_planner,
        "get_settings",
        lambda: SimpleNamespace(google_calendar_id="primary", commute_lookahead_days=7),
    )

    deleted = await delete_stray_commute_legs(None)

    assert deleted == 2
    assert sorted(deleted_ids) == ["past", "stray"]


@pytest.mark.asyncio
async def test_leg_conflict_alerts_once_and_clears(monkeypatch):
    async def fake_write(session, **kwargs):
        return None

    monkeypatch.setattr(commute_planner, "create_event", fake_write)
    monkeypatch.setattr(commute_planner, "patch_event", fake_write)
    monkeypatch.setattr(
        commute_planner, "get_settings", lambda: SimpleNamespace(reminder_lead_minutes=15),
    )

    leg = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-1", origin="Home", destination=GYM,
        mode="transit", depart=_day_at(18, 20), arrive=_day_at(18, 55),
    )
    blocker = ("ev-fixed", "Dinner")
    window = (_day_at(8), _day_at(23))

    # Fresh leg with a blocker → conflict reported once.
    _, _, conflicts = await commute_planner._write_legs(
        None, [leg], calendar_id="primary", existing=[], window=window,
        account_key=None, conflicts={leg.key: blocker},
    )
    assert conflicts == [(leg, "Dinner")]

    # Same conflict already marked on the existing leg → no re-alert.
    marked = _calendar_event(
        "leg-ev", leg.depart, leg.arrive,
        props={
            "managed_by": "plan_service", "kind": "commute",
            "origin_anchor": "home", "dest_anchor": "ev-1",
            "mode": "transit", "conflict": "ev-fixed",
        },
    )
    _, _, conflicts = await commute_planner._write_legs(
        None, [leg], calendar_id="primary", existing=[marked], window=window,
        account_key=None, conflicts={leg.key: blocker},
    )
    assert conflicts == []

    # Conflict resolved → notification tag cleared.
    cleared = []

    async def fake_clear(tag):
        cleared.append(tag)

    monkeypatch.setattr("app.services.notify.clear_notification_tag", fake_clear)
    _, _, conflicts = await commute_planner._write_legs(
        None, [leg], calendar_id="primary", existing=[marked], window=window,
        account_key=None, conflicts={},
    )
    assert conflicts == []
    assert cleared == ["conflict-home-ev-1"]


@pytest.mark.asyncio
async def test_duplicate_legs_are_deleted(monkeypatch):
    # Two calendar events with the same leg identity (a diff that couldn't
    # see the first copy created a second) — the surplus one is deleted and
    # the survivor patched.
    deleted = []

    async def fake_delete(session, *, calendar_id, event_id, account_key=None):
        deleted.append(event_id)

    async def fake_write(session, **kwargs):
        return None

    monkeypatch.setattr(commute_planner, "delete_event", fake_delete)
    monkeypatch.setattr(commute_planner, "patch_event", fake_write)
    monkeypatch.setattr(commute_planner, "create_event", fake_write)
    monkeypatch.setattr(
        commute_planner, "get_settings", lambda: SimpleNamespace(reminder_lead_minutes=15),
    )

    leg = PlannedLeg(
        origin_anchor="ev-1", dest_anchor="home", origin=GYM, destination="Home",
        mode="bicycling", depart=_day_at(23, 5), arrive=_day_at(23, 20),
    )
    props = {
        "managed_by": "plan_service", "kind": "commute",
        "origin_anchor": "ev-1", "dest_anchor": "home", "mode": "transit",
    }
    old_transit = _calendar_event("dup-a", _day_at(23, 5), _day_at(23, 45), props=dict(props))
    stray_bike = _calendar_event("dup-b", _day_at(23, 5), _day_at(23, 20), props=dict(props))

    await commute_planner._write_legs(
        None, [leg], calendar_id="primary", existing=[old_transit, stray_bike],
        window=(_day_at(8), _day_at(23, 59)), account_key=None,
    )

    assert deleted == ["dup-b"]


def test_immovable_conflicts_cover_online_events():
    from app.services.commute.planner import _immovable_conflicts

    # The dodge couldn't clear this location-less event — the leg overlaps
    # it and the user must be told, same as with a located anchor.
    leg = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-1", origin="Home", destination=GYM,
        mode="transit", depart=_day_at(18, 20), arrive=_day_at(18, 55),
    )
    obstacles = [
        ("ev-1", "Englischer", _day_at(19), _day_at(23)),      # own anchor — skipped
        ("ev-blob", "(untitled)", _day_at(16, 30), _day_at(18, 30)),
    ]
    assert _immovable_conflicts([leg], obstacles) == {leg.key: ("ev-blob", "(untitled)")}


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


def _calendar_event(
    event_id, start, end, *, location=None, all_day=False, props=None, transparency=None,
):
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
        raw={"transparency": transparency} if transparency else {},
    )


def test_replan_span_covers_timed_events_including_online():
    commute_props = {"managed_by": "plan_service", "kind": "commute"}
    events = [
        _calendar_event("gym", _day_at(14), _day_at(15), location=GYM),
        _calendar_event("office", _day_at(9), _day_at(10), location=OFFICE),
        _calendar_event("call", _day_at(11), _day_at(12), location="https://zoom.us/j/1"),
        _calendar_event("leg", _day_at(13), _day_at(13, 30), location=GYM, props=commute_props),
        _calendar_event("holiday", _day_at(0), _day_at(23), location=GYM, all_day=True),
    ]
    span = _replan_span(events)
    assert span == (_day_at(9), _day_at(15))

    # A changed online / location-less event still shapes legs (they dodge
    # it) — its span alone must trigger a replan.
    assert _replan_span([events[2]]) == (_day_at(11), _day_at(12))
    # Only the planner's own legs and all-day events don't.
    assert _replan_span([events[3], events[4]]) is None


def test_all_day_busy_event_triggers_overlap_with_task():
    from app.services.calendar.discover import _managed_overlaps

    task_props = {"managed_by": "plan_service", "kind": "task"}
    all_day = _calendar_event(
        "vacation", _day_at(0), _day_at(0, 0, day_offset=4), all_day=True,
    )
    task = _calendar_event("task-ev", _day_at(15), _day_at(16), props=task_props)

    assert _managed_overlaps(all_day, [task]) == [task]


@pytest.mark.asyncio
async def test_busy_view_blocks_all_day_busy_and_skips_free(monkeypatch):
    vacation = _calendar_event(
        "vacation", _day_at(0), _day_at(0, 0, day_offset=4), all_day=True,
    )
    birthday = _calendar_event(
        "birthday", _day_at(0), _day_at(0, 0, day_offset=4),
        all_day=True, transparency="transparent",
    )
    maybe_lunch = _calendar_event(
        "maybe-lunch", _day_at(12), _day_at(13), transparency="transparent",
    )
    meeting = _calendar_event("meeting", _day_at(9), _day_at(10))

    async def fake_list_events(session, *, calendar_ids, time_min, time_max, account_key=None):
        return [vacation, birthday, maybe_lunch, meeting]

    async def fake_resolve(_location):
        return None

    monkeypatch.setattr("app.services.calendar.list_events_between", fake_list_events)
    monkeypatch.setattr(schedule_service, "resolve_routable_location", fake_resolve)
    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(google_calendar_id="primary", google_busy_calendar_ids=[]),
    )

    busy = await schedule_service._fetch_busy_events(
        None, _day_at(8), _day_at(20), exclude_event_id=None, account_key=None,
    )

    by_id = {ev.id: ev for ev in busy}
    # Free events (all-day or timed) never block; busy all-day blocks the
    # whole day, midnight to midnight.
    assert set(by_id) == {"vacation", "meeting"}
    assert by_id["vacation"].start == _day_at(0)
    assert by_id["vacation"].end == _day_at(0, 0, day_offset=4)


@pytest.mark.asyncio
async def test_deleted_event_span_widens_replan_window(monkeypatch):
    from app.services.calendar import discover as discover_mod

    captured = {}

    async def fake_plan(session, *, window_start, window_end, account_key=None):
        captured["window"] = (window_start, window_end)

    monkeypatch.setattr(
        "app.services.plan.commute.plan_commutes_window_best_effort", fake_plan,
    )
    monkeypatch.setattr(
        "app.services.plan.commute.commute_window_margin", lambda: timedelta(hours=2),
    )
    monkeypatch.setattr(
        discover_mod, "get_settings", lambda: SimpleNamespace(commute_enabled=True),
    )

    deleted = (_day_at(12), _day_at(13))
    await discover_mod._plan_legs_for_changed_events(
        None, [], deleted_spans=[deleted], account_key=None,
    )

    assert captured["window"] == (
        deleted[0] - timedelta(hours=2),
        deleted[1] + timedelta(hours=2),
    )


@pytest.mark.asyncio
async def test_cancelled_event_span_recovery(monkeypatch):
    from app.services.calendar.discover import _cancelled_event_span

    # Recurring instance: the tombstone itself carries originalStartTime.
    span = await _cancelled_event_span(
        None, "cal", {"id": "x", "originalStartTime": {"dateTime": "2026-07-08T12:45:00+02:00"}},
        account_key=None,
    )
    assert span is not None
    assert span[0].isoformat() == "2026-07-08T12:45:00+02:00"

    # Plain deletion: the owner's cancelled copy is fetched and keeps times.
    tombstone = _calendar_event("gone", _day_at(12, 45), _day_at(13, 45))

    async def fake_get_event(session, *, calendar_id, event_id, account_key=None):
        return tombstone

    monkeypatch.setattr("app.services.calendar.get_event", fake_get_event)
    span = await _cancelled_event_span(None, "cal", {"id": "gone"}, account_key=None)
    assert span == (tombstone.start, tombstone.end)

    # Unrecoverable tombstone → None, the daily re-baseline heals instead.
    async def broken_get_event(session, *, calendar_id, event_id, account_key=None):
        raise KeyError("date")

    monkeypatch.setattr("app.services.calendar.get_event", broken_get_event)
    assert await _cancelled_event_span(None, "cal", {"id": "gone"}, account_key=None) is None


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


def test_reschedule_overlap_catches_too_close_foreign_leg():
    # A foreign leg re-derived to sit 5 minutes after the task violates the
    # event gap — the task must move, same as a true overlap.
    slot = Interval(_day_at(17, 30), _day_at(18, 15), "ev-1")
    close = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-2", origin="Home", destination=GYM,
        mode="transit", depart=_day_at(18, 20), arrive=_day_at(18, 55),
    )
    hit = _first_overlap(slot, [close], "ev-1")
    assert hit is not None and hit.start == close.depart

    # Exactly the event gap away is fine.
    clear = PlannedLeg(
        origin_anchor="home", dest_anchor="ev-2", origin="Home", destination=GYM,
        mode="transit", depart=_day_at(18, 30), arrive=_day_at(19, 5),
    )
    assert _first_overlap(slot, [clear], "ev-1") is None


@pytest.mark.asyncio
async def test_reschedule_lands_flush_after_own_vacated_slot(monkeypatch):
    # Overdue task, due 20:00, its old 18:00–19:00 slot blocked so it isn't
    # re-placed there. The vacated slot is not a real event: the task may
    # start flush at 19:00 and still make its deadline.
    tz = user_tz()
    day = (datetime.now(tz) + timedelta(days=1)).replace(second=0, microsecond=0)
    due = day.replace(hour=20, minute=0)
    prior = Interval(day.replace(hour=18, minute=0), day.replace(hour=19, minute=0), "ev-1")
    wall = BusyEvent(
        "wall", datetime.now(tz) - timedelta(hours=1), day.replace(hour=18, minute=0), "busy",
    )

    async def fake_fetch(session, time_min, time_max, **kwargs):
        return [wall]

    monkeypatch.setattr(schedule_service, "_fetch_busy_events", fake_fetch)
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy)

    task = SimpleNamespace(
        id=uuid.uuid4(),
        due_date=due,
        location=None,
        estimation=60,
        calendar_event_id="ev-1",
    )
    planned = await schedule_service.plan_task_slot(None, task, block=prior)

    assert planned.start == prior.end
    assert planned.end == due


@pytest.mark.asyncio
async def test_repair_passes_share_one_displacement_ledger(monkeypatch):
    # Normal-window and extended-window repair must consult the SAME ledger,
    # so a task attempted once is never re-planned in this call — that keeps
    # the displacement search linear instead of exponential.
    ledgers = []

    def fake_victims(session, task, busy, duration, gaps, ws, we, *, displaced=frozenset()):
        ledgers.append(displaced)
        return []

    tz = user_tz()
    due = (datetime.now(tz) + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    wall = BusyEvent("wall", datetime.now(tz) - timedelta(hours=1), due, "busy")

    async def fake_fetch(session, time_min, time_max, **kwargs):
        return [wall]

    monkeypatch.setattr(schedule_service, "_movable_victims", fake_victims)
    monkeypatch.setattr(schedule_service, "_fetch_busy_events", fake_fetch)
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy)

    task = SimpleNamespace(
        id=uuid.uuid4(), due_date=due, location=None, estimation=60, calendar_event_id=None,
    )
    with pytest.raises(ValueError):
        await schedule_service.plan_task_slot(None, task)

    assert len(ledgers) == 2  # normal + extended repair
    assert ledgers[0] is ledgers[1]
    assert task.id in ledgers[0]


@pytest.mark.asyncio
async def test_victim_may_shift_within_its_old_slot(monkeypatch):
    # 15-min task into a 105-min corridor holding a 45-min victim: only a
    # small victim shift fits (15+45+15+15+15 = 105). The victim's replan is
    # blocked around the NEW task's slot — not its own old span — so it may
    # slide inside it. And the victim is only moved once the slot is secured.
    krcmar = BusyEvent("krcmar", _day_at(10, 15), _day_at(11, 15), "busy")
    victim_ev = BusyEvent("unter", _day_at(11, 45), _day_at(12, 30), "task")
    ebay = BusyEvent("ebay", _day_at(13), _day_at(13, 45), "busy")
    victim_task = SimpleNamespace(
        id=uuid.uuid4(), due_date=_day_at(23, 45), location=None,
        estimation=45, calendar_event_id="unter",
    )
    freed = Interval(_day_at(11, 15), _day_at(13), "unter")

    def fake_victims(session, task, busy, duration, gaps, ws, we, *, displaced=frozenset()):
        return [(victim_task, victim_ev, freed)]

    captured = {}

    async def fake_locked(session, task, *, block, **kwargs):
        captured["block"] = block
        return PlannedSlot(block.start, block.end, block.start, block.end)

    async def fake_clear(task_id):
        return None

    monkeypatch.setattr(schedule_service, "_movable_victims", fake_victims)
    monkeypatch.setattr(schedule_service, "_schedule_task_locked", fake_locked)
    monkeypatch.setattr("app.services.notify.clear_task_notification", fake_clear)

    planned = await schedule_service._repair_by_displacing_task(
        None,
        SimpleNamespace(id=uuid.uuid4()),
        [krcmar, victim_ev, ebay],
        duration=timedelta(minutes=15),
        gaps=GAPS,
        out_s=0,
        in_s=0,
        window_start=_day_at(10),
        window_end=_day_at(23, 45),
        account_key=None,
        depth=0,
        displaced=set(),
        extended_window=False,
    )

    assert (planned.start, planned.end) == (_day_at(12, 30), _day_at(12, 45))
    # Victim exclusion zone = new slot ± event gap; its old span stays open.
    assert captured["block"].start == _day_at(12, 15)
    assert captured["block"].end == _day_at(13)


@pytest.mark.asyncio
async def test_no_slot_clears_stale_past_schedule(monkeypatch):
    tz = user_tz()

    async def raising_plan(*args, **kwargs):
        raise ValueError("no free slot before due date")

    async def fake_delete(session, task):
        return None

    async def fake_notify(task):
        return None

    monkeypatch.setattr(schedule_service, "plan_task_slot", raising_plan)
    monkeypatch.setattr("app.services.calendar.delete_task_event", fake_delete)
    monkeypatch.setattr("app.services.notify.notify_no_slot", fake_notify)
    monkeypatch.setattr("app.events.publish_task", lambda session, task_id: None)
    monkeypatch.setattr(
        schedule_service, "get_settings",
        lambda: SimpleNamespace(google_calendar_default_event_minutes=30),
    )
    session = SimpleNamespace(flush=lambda: None, commit=lambda: None)

    # Slot already in the past → cleared: no phantom schedule in the frontend.
    stale = SimpleNamespace(
        id=uuid.uuid4(), title="x", due_date=datetime.now(tz) + timedelta(days=1),
        scheduled_date=datetime.now(tz) - timedelta(hours=3),
        calendar_event_id="ev-old", estimation=60, location=None,
    )
    result = await schedule_service._schedule_task_locked(
        session, stale, block=None, account_key=None, notify=True, _depth=0,
    )
    assert result is None
    assert stale.scheduled_date is None
    assert stale.calendar_event_id is None

    # Future (merely conflicting) slot → kept.
    future = SimpleNamespace(
        id=uuid.uuid4(), title="y", due_date=datetime.now(tz) + timedelta(days=2),
        scheduled_date=datetime.now(tz) + timedelta(hours=3),
        calendar_event_id="ev-live", estimation=60, location=None,
    )
    result = await schedule_service._schedule_task_locked(
        session, future, block=None, account_key=None, notify=True, _depth=0,
    )
    assert result is None
    assert future.scheduled_date is not None
    assert future.calendar_event_id == "ev-live"


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
    monkeypatch.setattr(schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy)

    task = SimpleNamespace(
        id=uuid.uuid4(),
        due_date=due,
        location=None,
        estimation=60,
        calendar_event_id=None,
    )
    planned = await schedule_service.plan_task_slot(None, task)
    assert planned.end <= meeting.start - EVENT_BUFFER
