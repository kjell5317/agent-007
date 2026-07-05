from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.commute.legs import (  # noqa: E402
    FAILED_LEG_SECONDS,
    FAILED_MODE,
    HOME,
    Anchor,
    derive_legs,
    required_routes,
)
from app.timezones import to_user_tz  # noqa: E402

HOME_ADDR = "Homestreet 1, Munich"
GYM = "Gymstreet 5, Munich"
OFFICE = "Officeplatz 2, Munich"

SETTINGS = SimpleNamespace(
    commute_bike_max_minutes=25,
    commute_rain_threshold_pct=30,
    commute_home_layover_minutes=60,
    commute_event_buffer_minutes=5,
    event_buffer_minutes=15,
)

BUFFER = timedelta(minutes=5)
EVENT_BUFFER = timedelta(minutes=15)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 10, hour, minute, tzinfo=timezone.utc)


def _durations(overrides: dict | None = None) -> dict:
    base = {
        (HOME_ADDR, GYM, "bicycling"): 600,
        (GYM, HOME_ADDR, "bicycling"): 600,
        (HOME_ADDR, OFFICE, "bicycling"): 900,
        (OFFICE, HOME_ADDR, "bicycling"): 900,
        (GYM, OFFICE, "bicycling"): 300,
        (OFFICE, GYM, "bicycling"): 300,
    }
    base.update(overrides or {})
    return base


def test_single_anchor_round_trip():
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    legs, skipped = derive_legs([anchor], HOME_ADDR, _durations(), None, SETTINGS)

    assert skipped == 0
    assert [leg.key for leg in legs] == [(HOME, "ev1"), ("ev1", HOME)]
    outbound, inbound = legs
    assert outbound.arrive == anchor.start - BUFFER
    assert outbound.depart == outbound.arrive - timedelta(seconds=600)
    assert outbound.mode == "bicycling"
    assert inbound.depart == anchor.end + BUFFER
    assert inbound.arrive == inbound.depart + timedelta(seconds=600)


def test_same_location_anchors_share_one_trip():
    first = Anchor("ev1", _at(14), _at(15), GYM)
    second = Anchor("ev2", _at(15, 30), _at(16), GYM)
    legs, _ = derive_legs([first, second], HOME_ADDR, _durations(), None, SETTINGS)

    assert [leg.key for leg in legs] == [(HOME, "ev1"), ("ev2", HOME)]


def test_short_gap_gets_direct_leg():
    first = Anchor("ev1", _at(14), _at(15), GYM)
    second = Anchor("ev2", _at(15, 45), _at(17), OFFICE)
    legs, _ = derive_legs([first, second], HOME_ADDR, _durations(), None, SETTINGS)

    keys = [leg.key for leg in legs]
    assert keys == [(HOME, "ev1"), ("ev1", "ev2"), ("ev2", HOME)]
    direct = legs[1]
    assert direct.origin == GYM
    assert direct.destination == OFFICE
    assert direct.arrive == second.start - BUFFER
    assert direct.depart == direct.arrive - timedelta(seconds=300)


def test_long_gap_goes_via_home():
    first = Anchor("ev1", _at(10), _at(11), GYM)
    second = Anchor("ev2", _at(16), _at(17), OFFICE)
    legs, _ = derive_legs([first, second], HOME_ADDR, _durations(), None, SETTINGS)

    keys = [leg.key for leg in legs]
    assert keys == [
        (HOME, "ev1"),
        ("ev1", HOME),
        (HOME, "ev2"),
        ("ev2", HOME),
    ]


def test_rain_flips_bike_to_transit():
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    durations = _durations({(HOME_ADDR, GYM, "transit"): 1200, (GYM, HOME_ADDR, "transit"): 1200})
    rain = {
        to_user_tz(anchor.start).strftime("%Y-%m-%dT%H:00"): 80,
        to_user_tz(anchor.end).strftime("%Y-%m-%dT%H:00"): 80,
    }
    legs, _ = derive_legs([anchor], HOME_ADDR, durations, rain, SETTINGS)

    outbound = legs[0]
    assert outbound.mode == "transit"
    assert "rain" in outbound.reason
    assert outbound.depart == anchor.start - BUFFER - timedelta(seconds=1200)


def test_bike_over_threshold_uses_transit_and_never_bikes_back():
    far = "Farawaystrasse 9"
    durations = {
        (HOME_ADDR, far, "bicycling"): 3600,  # over 25-minute cap
        (HOME_ADDR, far, "transit"): 1800,
        (far, HOME_ADDR, "bicycling"): 3600,
        # No inbound transit — but the bike stayed home, so no bike fallback:
        # the leg becomes a failed placeholder instead.
    }
    anchor = Anchor("ev1", _at(14), _at(15), far)
    legs, skipped = derive_legs([anchor], HOME_ADDR, durations, None, SETTINGS)

    assert skipped == 1
    outbound, inbound = legs
    assert outbound.mode == "transit"
    assert inbound.mode == FAILED_MODE
    assert inbound.reason == "no route found"


def test_rain_on_return_sends_whole_trip_to_transit():
    # Bike out at 14:00, rain at the 15:00 ride home: a transit return would
    # strand the bike, so the whole trip goes onto transit.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    durations = _durations({(HOME_ADDR, GYM, "transit"): 1200, (GYM, HOME_ADDR, "transit"): 1200})
    rain = {to_user_tz(anchor.end).strftime("%Y-%m-%dT%H:00"): 80}
    legs, skipped = derive_legs([anchor], HOME_ADDR, durations, rain, SETTINGS)

    assert skipped == 0
    assert [leg.mode for leg in legs] == ["transit", "transit"]
    outbound = legs[0]
    assert outbound.reason == "one mode per trip — transit throughout"
    assert outbound.arrive == anchor.start - BUFFER
    assert outbound.depart == outbound.arrive - timedelta(seconds=1200)


def test_harmonize_records_missing_transit_and_converges():
    # Rain on the return flips it to transit, but the outbound transit was
    # never fetched — recorded for the lazy fetch, then the re-derive lands
    # the whole trip on transit.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    durations = _durations({(GYM, HOME_ADDR, "transit"): 1200})
    rain = {to_user_tz(anchor.end).strftime("%Y-%m-%dT%H:00"): 80}

    missing: set[tuple[str, str]] = set()
    derive_legs([anchor], HOME_ADDR, durations, rain, SETTINGS, missing_transit=missing)
    assert missing == {(HOME_ADDR, GYM)}

    durations[(HOME_ADDR, GYM, "transit")] = 1100
    legs, _ = derive_legs([anchor], HOME_ADDR, durations, rain, SETTINGS)
    assert [leg.mode for leg in legs] == ["transit", "transit"]


def test_no_outbound_transit_keeps_bike_round_trip_despite_rain():
    # Transit can't cover the outbound (definitive no-route), so the trip
    # keeps the bike both ways even though rain wanted transit home.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    durations = _durations({
        (HOME_ADDR, GYM, "transit"): None,
        (GYM, HOME_ADDR, "transit"): 1200,
    })
    rain = {to_user_tz(anchor.end).strftime("%Y-%m-%dT%H:00"): 80}
    legs, skipped = derive_legs([anchor], HOME_ADDR, durations, rain, SETTINGS)

    assert skipped == 0
    assert [leg.mode for leg in legs] == ["bicycling", "bicycling"]
    inbound = legs[1]
    assert inbound.reason == "no transit for the whole trip — keeping the bike"
    assert inbound.depart == anchor.end + BUFFER
    assert inbound.arrive == inbound.depart + timedelta(seconds=600)


def test_transit_fallback_to_bike_still_works_when_bike_is_along():
    # Rain wants transit for the ride home, but no transit route exists.
    # The outbound leg was ridden, so the bike is on hand to fall back to.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    rain = {to_user_tz(anchor.end).strftime("%Y-%m-%dT%H:00"): 80}
    legs, skipped = derive_legs([anchor], HOME_ADDR, _durations(), rain, SETTINGS)

    assert skipped == 0
    outbound, inbound = legs
    assert outbound.mode == "bicycling"
    assert inbound.mode == "bicycling"
    assert inbound.reason == "transit unavailable, fell back to bike"


def test_no_bike_leg_after_leaving_home_by_transit():
    # home -transit-> far -?-> nearby: the bike is at home, so the short
    # second hop must not be ridden even though it fits the bike threshold.
    far = "Farawaystrasse 9"
    nearby = "Nebenstrasse 2"
    durations = {
        (HOME_ADDR, far, "bicycling"): 3600,  # over cap → transit out
        (HOME_ADDR, far, "transit"): 1800,
        (far, nearby, "bicycling"): 300,
        (far, nearby, "transit"): 900,
        (nearby, HOME_ADDR, "bicycling"): 600,
        (nearby, HOME_ADDR, "transit"): 1500,
    }
    first = Anchor("ev1", _at(14), _at(15), far)
    second = Anchor("ev2", _at(15, 45), _at(17), nearby)
    legs, skipped = derive_legs([first, second], HOME_ADDR, durations, None, SETTINGS)

    assert skipped == 0
    assert [leg.key for leg in legs] == [(HOME, "ev1"), ("ev1", "ev2"), ("ev2", HOME)]
    assert [leg.mode for leg in legs] == ["transit", "transit", "transit"]
    assert legs[1].reason == "bike not along on this trip"
    assert legs[2].reason == "bike not along on this trip"


def test_bike_chain_stays_bike_and_resets_at_home():
    # Ridden chain gym→office stays on the bike; after a via-home layover
    # the next departure gets the bike again even if an earlier chain broke.
    first = Anchor("ev1", _at(10), _at(11), GYM)
    second = Anchor("ev2", _at(16), _at(17), OFFICE)
    legs, _ = derive_legs([first, second], HOME_ADDR, _durations(), None, SETTINGS)

    assert [leg.mode for leg in legs] == ["bicycling"] * 4


def test_unroutable_legs_become_30min_placeholders():
    anchor = Anchor("ev1", _at(14), _at(15), "Nowhere 1")
    legs, unroutable = derive_legs([anchor], HOME_ADDR, {}, None, SETTINGS)

    assert unroutable == 2
    outbound, inbound = legs
    assert outbound.mode == FAILED_MODE
    assert outbound.reason == "no route found"
    assert outbound.arrive - outbound.depart == timedelta(seconds=FAILED_LEG_SECONDS)
    assert outbound.arrive == anchor.start - BUFFER
    assert inbound.mode == FAILED_MODE
    assert inbound.depart == anchor.end + BUFFER


def test_unroutable_middle_gap_gets_failed_direct_leg():
    first = Anchor("ev1", _at(14), _at(15), "Nowhere 1")
    second = Anchor("ev2", _at(15, 45), _at(17), "Nowhere 2")
    legs, unroutable = derive_legs([first, second], HOME_ADDR, {}, None, SETTINGS)

    assert unroutable == 3
    direct = next(leg for leg in legs if leg.key == ("ev1", "ev2"))
    assert direct.mode == FAILED_MODE
    assert direct.arrive - direct.depart == timedelta(seconds=FAILED_LEG_SECONDS)


def test_return_leg_departs_after_latest_ending_overlap():
    # Double-booked evening: a short event starts inside a longer one. The
    # ride home leaves after the *longest* event ends — not after the
    # later-starting short one.
    long_ev = Anchor("ev-long", _at(18), _at(22), GYM)
    short_ev = Anchor("ev-short", _at(18, 10), _at(18, 40), OFFICE)
    legs, skipped = derive_legs([long_ev, short_ev], HOME_ADDR, _durations(), None, SETTINGS)

    assert skipped == 0
    assert [leg.key for leg in legs] == [(HOME, "ev-long"), ("ev-long", HOME)]
    inbound = legs[-1]
    assert inbound.origin == GYM
    assert inbound.depart == long_ev.end + BUFFER


def test_overlap_cluster_required_routes_use_entry_and_exit():
    long_ev = Anchor("ev-long", _at(18), _at(22), GYM)
    short_ev = Anchor("ev-short", _at(18, 10), _at(18, 40), OFFICE)
    routes = required_routes([long_ev, short_ev], HOME_ADDR)

    assert set(routes) == {(HOME_ADDR, GYM), (GYM, HOME_ADDR)}


def test_direct_leg_dodges_online_meeting():
    # Physical → physical with an online meeting right before the second
    # event: the connecting leg slides into the free gap instead of sitting
    # on top of the call.
    first = Anchor("ev1", _at(14), _at(15), GYM)
    second = Anchor("ev2", _at(16, 30), _at(18), OFFICE)
    online = (_at(15, 30), _at(16, 25))
    legs, _ = derive_legs(
        [first, second], HOME_ADDR, _durations(), None, SETTINGS, avoid=[online],
    )

    direct = next(leg for leg in legs if leg.key == ("ev1", "ev2"))
    # The online meeting is not the leg's anchor — the full event gap applies.
    assert direct.arrive == online[0] - EVENT_BUFFER
    assert direct.depart == direct.arrive - timedelta(seconds=300)
    assert direct.depart >= first.end + BUFFER


def test_home_leg_dodges_busy_event_before_anchor():
    # A location-less busy event sits where the just-in-time ride would go —
    # the leg leaves home earlier and waits at the destination instead.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    avoid = [(_at(13, 30), _at(13, 50))]
    legs, _ = derive_legs([anchor], HOME_ADDR, _durations(), None, SETTINGS, avoid=avoid)

    outbound = legs[0]
    assert outbound.arrive == avoid[0][0] - EVENT_BUFFER
    assert outbound.depart == outbound.arrive - timedelta(seconds=600)


def test_home_leg_dodge_is_capped():
    # Clearing the span would mean leaving hours early — beyond the cap the
    # leg stays just-in-time and the overlap stays visible.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    avoid = [(_at(10), _at(13, 50))]
    legs, _ = derive_legs([anchor], HOME_ADDR, _durations(), None, SETTINGS, avoid=avoid)

    outbound = legs[0]
    assert outbound.arrive == anchor.start - BUFFER


def test_home_leg_departs_after_online_meeting():
    # An online meeting sits right after the event: attend it there and ride
    # home afterwards instead of riding through it. The meeting is the
    # trip's effective last stop, so only the inner commute gap applies.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    avoid = [(_at(15, 10), _at(16))]
    legs, _ = derive_legs([anchor], HOME_ADDR, _durations(), None, SETTINGS, avoid=avoid)

    inbound = legs[1]
    assert inbound.depart == avoid[0][1] + BUFFER
    assert inbound.arrive == inbound.depart + timedelta(seconds=600)


def test_home_leg_stays_put_when_later_dodge_exceeds_cap():
    # Waiting three hours to leave costs more than the visible overlap.
    anchor = Anchor("ev1", _at(14), _at(15), GYM)
    avoid = [(_at(15, 10), _at(18))]
    legs, _ = derive_legs([anchor], HOME_ADDR, _durations(), None, SETTINGS, avoid=avoid)

    inbound = legs[1]
    assert inbound.depart == anchor.end + BUFFER


def test_direct_leg_keeps_late_placement_when_dodge_cannot_fit():
    # The online meeting fills the whole gap — nothing earlier clears, so
    # the leg stays just-in-time (visible overlap beats being late).
    first = Anchor("ev1", _at(14), _at(15), GYM)
    second = Anchor("ev2", _at(16, 30), _at(18), OFFICE)
    online = (_at(15), _at(16, 30))
    legs, _ = derive_legs(
        [first, second], HOME_ADDR, _durations(), None, SETTINGS, avoid=[online],
    )

    direct = next(leg for leg in legs if leg.key == ("ev1", "ev2"))
    assert direct.arrive == second.start - BUFFER
    assert direct.depart == direct.arrive - timedelta(seconds=300)


def test_missing_transit_pairs_are_reported_for_lazy_fetch():
    # Chain-forced transit on pairs the optimistic resolver never fetched:
    # the first derive reports them, and a re-derive with the fetched
    # durations rides transit throughout.
    far = "Farawaystrasse 9"
    nearby = "Nebenstrasse 2"
    durations = {
        (HOME_ADDR, far, "bicycling"): 3600,  # over cap → transit out
        (HOME_ADDR, far, "transit"): 1800,
        (HOME_ADDR, nearby, "bicycling"): 600,
        (far, nearby, "bicycling"): 300,
        (nearby, HOME_ADDR, "bicycling"): 600,
    }
    first = Anchor("ev1", _at(14), _at(15), far)
    second = Anchor("ev2", _at(15, 45), _at(17), nearby)

    missing: set[tuple[str, str]] = set()
    derive_legs(
        [first, second], HOME_ADDR, durations, None, SETTINGS, missing_transit=missing,
    )
    assert missing == {(far, HOME_ADDR), (far, nearby), (nearby, HOME_ADDR)}

    durations[(far, HOME_ADDR, "transit")] = 2000
    durations[(far, nearby, "transit")] = 900
    durations[(nearby, HOME_ADDR, "transit")] = 1500
    legs, skipped = derive_legs([first, second], HOME_ADDR, durations, None, SETTINGS)

    assert skipped == 0
    assert [leg.mode for leg in legs] == ["transit", "transit", "transit"]


def test_home_anchor_produces_no_legs():
    anchor = Anchor("ev1", _at(14), _at(15), HOME_ADDR.upper())
    legs, skipped = derive_legs([anchor], HOME_ADDR, _durations(), None, SETTINGS)

    assert legs == []
    assert skipped == 0


def test_required_routes_cover_home_and_chain_pairs():
    first = Anchor("ev1", _at(14), _at(15), GYM)
    second = Anchor("ev2", _at(15, 45), _at(17), OFFICE)
    routes = required_routes([first, second], HOME_ADDR)

    assert set(routes) == {
        (HOME_ADDR, GYM),
        (GYM, HOME_ADDR),
        (HOME_ADDR, OFFICE),
        (OFFICE, HOME_ADDR),
        (GYM, OFFICE),
    }
    assert routes[(HOME_ADDR, GYM)] == first.start
    assert routes[(GYM, OFFICE)] == first.end
