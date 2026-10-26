from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.commute import planner as commute_planner  # noqa: E402
from app.services.commute.legs import FAILED_MODE, PlannedLeg  # noqa: E402
from app.services.commute.planner import _description_for, _navigation_url  # noqa: E402
from app.timezones import user_tz  # noqa: E402

GYM = "Gymstreet 5, Munich"
OFFICE = "Officeplatz 2, Munich"


def _day_at(hour: int, minute: int = 0, day_offset: int = 3) -> datetime:
    base = datetime.now(user_tz()) + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _url_query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


def _calendar_event(
    event_id,
    start,
    end,
    *,
    location=None,
    all_day=False,
    props=None,
    transparency=None,
    raw=None,
):
    raw_payload = dict(raw or {})
    if transparency:
        raw_payload["transparency"] = transparency
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
        raw=raw_payload,
    )


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

    # Title is the mode emoji — destination lives in the location field.
    assert _summary_for(leg) == "🚆 Travel"
    assert _summary_for(replace_leg(leg, mode="bicycling")) == "🚲 Travel"
    assert _summary_for(replace_leg(leg, mode=FAILED_MODE)) == "⚠️ No route"

    # The Maps deep link ignores departure_time — never send it.
    query = _url_query(_navigation_url(leg))
    assert query["travelmode"] == ["transit"]
    assert "departure_time" not in query

    desc = _description_for(leg)
    lines = desc.splitlines()
    assert lines[0] == "From: Home"
    assert not any(line.startswith(("To:", "Mode:", "Reason:")) for line in lines)
    assert any(line.startswith('<a href="https://www.google.com/maps/dir/?') for line in lines)
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


@pytest.mark.asyncio
async def test_stray_leg_cleanup_removes_past_and_beyond_horizon(monkeypatch):
    from app.services.commute.planner import delete_stray_commute_legs

    now = datetime.now(timezone.utc)
    def leg(event_id, start, end):
        props = {"managed_by": "plan_service", "kind": "commute", "origin_anchor": "home",
                 "dest_anchor": f"anchor-{event_id}", "mode": "bicycling"}
        return _calendar_event(event_id, start, end, props=props)

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

    folded = []
    monkeypatch.setattr(commute_planner, "list_events_between", fake_list)
    monkeypatch.setattr(commute_planner, "delete_event", fake_delete)
    monkeypatch.setattr(
        "app.services.commute.state.fold_finished", lambda _session, legs: folded.extend(legs)
    )
    monkeypatch.setattr(
        commute_planner,
        "get_settings",
        lambda: SimpleNamespace(google_calendar_id="primary", commute_lookahead_days=7),
    )

    deleted = await delete_stray_commute_legs(SimpleNamespace(commit=lambda: None))

    assert deleted == 2
    assert sorted(deleted_ids) == ["past", "stray"]
    # The finished ride is folded into the stored position before it goes.
    assert [leg.depart for leg in folded] == [past_leg.start]


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
