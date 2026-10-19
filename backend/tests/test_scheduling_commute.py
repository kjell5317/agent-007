"""Commute inside the solver: routes, the roaming bike, soft rules, pins."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.commute.state import LegRecord, derive_state
from app.services.learn import data
from app.services.plan import features as F
from app.services.plan import solver as S

DAY, HOUR = S.SLOTS_PER_DAY, S.SLOTS_PER_HOUR
W = F.prior_mean()
HOME, UNI, LIB = 0, 1, 2


def at(day: int, hour: float) -> int:
    return day * DAY + round(hour * HOUR)


def matrix(pairs: dict[tuple[int, int], tuple[int | None, int | None]]):
    """Symmetric travel (slots) plus prior leg utilities."""
    travel, utility = {}, {}
    for (a, b), (bike, transit) in pairs.items():
        for x, y in ((a, b), (b, a)):
            travel[(x, y)] = (bike, transit)
            if bike is not None:
                utility[(x, y, "bicycling")] = F.utility(F.leg_features("bicycling", bike * 5), W)
            if transit is not None:
                utility[(x, y, "transit")] = F.utility(F.leg_features("transit", transit * 5), W)
    return travel, utility


def rain(hours: range, pct: float = 70, mm: float = 2.0, days: int = 2) -> list[float]:
    out = [0.0] * ((days + 1) * 24)
    for h in hours:
        out[h] = F.utility(F.ride_weather_features(F.LegWeather(rain_pct=pct, rain_mm=mm), 30), W)
    return out


def solve(commute: S.CommuteInput, tasks=(), fixed=(), days=2) -> S.SolveResult:
    inp = S.SolveInput(
        days=[date(2026, 10, 19) + timedelta(days=d) for d in range(days)],
        tasks=list(tasks),
        fixed=list(fixed),
        fixed_load=[0.0] * days,
        commute=commute,
        workers=1,
        time_limit=10,
    )
    result = S.solve(inp)
    assert result.ok and result.routes is not None
    return result


def lecture(day: int, start: float, end: float, key: str = "lec", loc: int = UNI):
    return S.RouteNode(key, key, at(day, start), at(day, end), loc), S.FixedItem(
        at(day, start), at(day, end), "event"
    )


def legs(result, day=0):
    return [(leg.origin_key, leg.dest_key, leg.mode) for leg in result.routes if leg.day == day]


def commute_input(**kw) -> S.CommuteInput:
    travel, utility = matrix({(HOME, UNI): (3, 5), (HOME, LIB): (4, 6), (UNI, LIB): (2, 4)})
    base = dict(travel=travel, leg_utility=utility, ride_hour_utility=rain(range(0)), now=at(0, 7))
    base.update(kw)
    return S.CommuteInput(**base)


def test_dry_day_rides_there_and_back():
    node, item = lecture(0, 10, 12)
    result = solve(commute_input(fixed=[node]), fixed=[item])
    assert legs(result) == [("home", "lec", "bicycling"), ("lec", "home", "bicycling")]
    out, back = [leg for leg in result.routes if leg.day == 0]
    # Buffers sit inside the leg; the arrival leg ends before the lecture.
    assert out.end <= at(0, 10) and back.start >= at(0, 12)


def test_rain_on_the_way_home_leaves_the_bike_and_fetches_it_later():
    node, item = lecture(0, 10, 12)
    node2, item2 = lecture(1, 10, 12, key="lec2")
    # Rain from noon to the next noon: riding out on the dry morning and
    # back on the dry next afternoon beats transit four times, even though
    # the bike spends a night at Uni.
    result = solve(
        commute_input(fixed=[node, node2], ride_hour_utility=rain(range(12, 36))),
        fixed=[item, item2],
    )
    assert legs(result, 0) == [("home", "lec", "bicycling"), ("lec", "home", "transit")]
    # Day 2: the bike is at Uni — transit there, ride it back.
    assert legs(result, 1) == [("home", "lec2", "transit"), ("lec2", "home", "bicycling")]
    assert result.routes[1].bike_after == UNI


def test_bike_only_ridden_from_where_it_is():
    node, item = lecture(0, 10, 12)
    result = solve(commute_input(fixed=[node], bike_start=LIB), fixed=[item])
    first = result.routes[0]
    assert first.mode == "transit" and first.bike_before == LIB


def test_long_ride_is_a_cost_not_a_ban():
    # A 30-min ride (over the 25-min threshold) against 80 min of transit
    # rides; against 35 min of transit it doesn't.
    for transit, expected in ((16, "bicycling"), (7, "transit")):
        travel, utility = matrix({(HOME, UNI): (6, transit)})
        node, item = lecture(0, 10, 12)
        result = solve(commute_input(travel=travel, leg_utility=utility, fixed=[node]), fixed=[item])
        assert result.routes[0].mode == expected


def test_pin_is_honoured_when_reachable():
    node, item = lecture(0, 10, 12)
    commute = commute_input(fixed=[node], pins={("home", "lec"): "transit"})
    result = solve(commute, fixed=[item])
    assert result.routes[0].mode == "transit" and result.routes[0].pinned


def test_located_task_routes_via_its_location():
    node, item = lecture(0, 10, 12)
    task = S.SolverTask(key="t", dur=12, earliest=at(0, 8), due=at(0, 22), hint=at(0, 14))
    result = solve(
        commute_input(fixed=[node], task_locations={"t": LIB}, task_anchors={"t": "ev-t"}),
        tasks=[task], fixed=[item],
    )
    keys = [(a, b) for a, b, _ in legs(result)]
    assert ("ev-t" in {a for a, _ in keys} | {b for _, b in keys})
    start = result.starts["t"]
    arrival = next(leg for leg in result.routes if leg.dest_key == "ev-t")
    assert arrival.end <= start


def test_legs_never_overlap_an_online_meeting():
    node, item = lecture(0, 10, 12)
    online = S.FixedItem(at(0, 9), at(0, 10), "event")  # online right before the lecture
    result = solve(commute_input(fixed=[node]), fixed=[item, online])
    arrival = result.routes[0]
    assert arrival.end <= at(0, 9) or arrival.start >= at(0, 10)
    assert arrival.shortfall == 0


def test_leg_shortened_only_between_fixed_items():
    a, item_a = lecture(0, 10, 11, key="a", loc=UNI)
    b, item_b = lecture(0, 11, 12, key="b", loc=LIB)
    result = solve(commute_input(fixed=[a, b]), fixed=[item_a, item_b])
    between = next(leg for leg in result.routes if leg.dest_key == "b")
    assert between.shortfall > 0


def test_long_gap_goes_home_short_gap_waits():
    for second_start, home_stop in ((17, True), (11, False)):
        a, item_a = lecture(0, 9, 10, key="a")
        b, item_b = lecture(0, second_start, second_start + 1, key="b")
        result = solve(commute_input(fixed=[a, b]), fixed=[item_a, item_b])
        assert (("a", "home") in {(o, d) for o, d, _ in legs(result)}) is home_stop


def test_state_from_legs():
    now = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    def leg(o, d, mode, depart, minutes=20, dest="Uni, Somewhere"):
        return LegRecord(o, d, None, dest if d != "home" else None, mode, depart,
                         depart + timedelta(minutes=minutes))
    records = [
        leg("home", "lec", "bicycling", now - timedelta(days=1, hours=6)),
        leg("lec", "home", "transit", now - timedelta(days=1, hours=2)),
        leg("home", "sem", "transit", now - timedelta(hours=1), dest="Office, X"),
        leg("sem", "home", "transit", now + timedelta(hours=2)),
    ]
    state = derive_state(records, now)
    assert state.bike_location == "Uni, Somewhere"
    assert state.user_location == "Office, X" and state.user_anchor == "sem"
    assert ("sem", "home") in state.upcoming


def test_flip_becomes_a_pairwise_row():
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    flip = SimpleNamespace(
        created_at=now,
        features={
            "planned": F.leg_features("bicycling", 30, weather=F.LegWeather(rain_pct=40)),
            "flipped": F.leg_features("transit", 40),
        },
    )
    [row] = data.commute_rows([flip], now)
    assert row.y == 1 and row.x["cm_transit"] == 1 and row.x["cm_rain:3"] == -1


def test_commute_features_carry_the_old_thresholds_as_prior():
    ride = F.utility(F.leg_features("bicycling", 30), W)
    transit = F.utility(F.leg_features("transit", 40), W)
    assert transit > ride  # today: 30 min > 25 min threshold → transit
    wet = F.utility(F.ride_weather_features(F.LegWeather(rain_pct=30), 30), W)
    dry = F.utility(F.ride_weather_features(F.LegWeather(rain_pct=5), 30), W)
    assert dry - wet == 1.0


def test_finished_legs_fold_into_the_stored_position_once():
    from app.services.commute.state import Position, advance

    now = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    ride = LegRecord("home", "lec", None, "Uni, Somewhere", "bicycling",
                     now - timedelta(days=1, hours=6), now - timedelta(days=1, hours=5, minutes=40))
    home = LegRecord("lec", "home", "Uni, Somewhere", None, "transit",
                     now - timedelta(days=1, hours=2), now - timedelta(days=1, hours=1, minutes=35))
    stored = advance(Position(), [ride, home])
    assert stored.bike_location == "Uni, Somewhere" and stored.user_location is None
    # Folding again (a delete that failed last hour) changes nothing.
    assert advance(stored, [ride, home]) == stored
    # Today's started leg replays on top of the stored position.
    today = LegRecord("home", "sem", None, "Office, X", "transit", now - timedelta(hours=1),
                      now - timedelta(minutes=30))
    state = derive_state([today], now, stored)
    assert state.bike_location == "Uni, Somewhere"
    assert state.user_location == "Office, X"
    # Yesterday's arrival doesn't place the user anywhere today.
    assert derive_state([], now, stored).user_location is None
