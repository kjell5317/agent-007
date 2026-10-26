from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.services.commute import client as maps  # noqa: E402
from app.services.commute import transit  # noqa: E402
from app.services.commute.client import MapsLookupError, TransitConnection  # noqa: E402
from app.services.commute.legs import PlannedLeg  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 26, 7, 0, tzinfo=TZ)
BUFFER = timedelta(minutes=5)

# Walk 4 min, bus, transfer walk, train, walk 3 min.
ROUTE = {"routes": [{"legs": [{"steps": [
    {"staticDuration": "120s"},
    {"staticDuration": "120s"},
    {"staticDuration": "600s", "transitDetails": {
        "stopDetails": {"departureStop": {"name": "Hauptbahnhof"},
                        "departureTime": "2026-10-26T07:12:00Z", "arrivalTime": "2026-10-26T07:22:00Z"},
        "transitLine": {"name": "Buslinie 100", "nameShort": "100"}}},
    {"staticDuration": "180s"},
    {"staticDuration": "900s", "transitDetails": {
        "stopDetails": {"departureStop": {"name": "Ostkreuz"},
                        "departureTime": "2026-10-26T07:27:00Z", "arrivalTime": "2026-10-26T07:42:00Z"},
        "transitLine": {"name": "S-Bahn S5"}}},
    {"staticDuration": "180.5s"},
]}]}]}


def at(hour: int, minute: int = 0) -> datetime:
    return NOW.replace(hour=hour, minute=minute)


def conn(depart: datetime, arrive: datetime) -> TransitConnection:
    return TransitConnection(depart=depart, arrive=arrive, boards=depart + timedelta(minutes=4),
                             line="S1", stop="Hauptbahnhof")


def leg(depart: datetime, arrive: datetime, mode: str = "transit", note: str | None = None) -> PlannedLeg:
    return PlannedLeg("home", "lec", "Home", "Uni", mode, depart, arrive, note=note)


def test_parse_takes_door_to_door_times_and_the_first_line():
    c = maps.parse_transit_route(ROUTE)
    assert c.depart == datetime(2026, 10, 26, 7, 8, tzinfo=timezone.utc)
    assert c.arrive == datetime(2026, 10, 26, 7, 45, 0, 500000, tzinfo=timezone.utc)
    assert c.boards == datetime(2026, 10, 26, 7, 12, tzinfo=timezone.utc)
    assert (c.line, c.stop) == ("100", "Hauptbahnhof")


def test_parse_without_a_ride_or_timetable_is_none():
    assert maps.parse_transit_route({}) is None
    assert maps.parse_transit_route({"routes": [{"legs": [{"steps": [{"staticDuration": "600s"}]}]}]}) is None
    untimed = {"routes": [{"legs": [{"steps": [{"transitDetails": {"stopDetails": {}}}]}]}]}
    assert maps.parse_transit_route(untimed) is None


@pytest.mark.asyncio
async def test_request_asks_for_an_arrive_by_transit_route(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(200, json=ROUTE)

    real = httpx.AsyncClient
    monkeypatch.setattr(maps.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(maps, "get_settings", lambda: SimpleNamespace(google_maps_api_key="key"))

    c = await maps.transit_connection(origin="Home", destination="Uni", arrive_by=at(8, 50))

    assert c is not None
    assert seen["body"]["travelMode"] == "TRANSIT"
    assert seen["body"]["arrivalTime"] == "2026-10-26T07:50:00Z"
    assert "departureTime" not in seen["body"]
    assert seen["headers"]["X-Goog-Api-Key"] == "key"
    assert "transitDetails" in seen["headers"]["X-Goog-FieldMask"]


@pytest.mark.asyncio
async def test_request_error_raises(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(maps.httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda _r: httpx.Response(403, json={"error": {}})), **kw))
    monkeypatch.setattr(maps, "get_settings", lambda: SimpleNamespace(google_maps_api_key="key"))
    with pytest.raises(MapsLookupError):
        await maps.transit_connection(origin="Home", destination="Uni", depart_at=at(8))


def test_free_window_is_bounded_by_the_neighbours():
    modelled = leg(at(8, 5), at(8, 40))
    others = [(at(6), at(7)), (at(7, 30), at(8)), (at(9), at(10)), (at(11), at(12))]
    assert transit.free_window(modelled, others) == (at(8), at(9))
    assert transit.free_window(modelled, []) == (None, None)
    assert transit.free_window(modelled, [(at(8, 30), at(9))]) is None


def test_pick_prefers_the_modelled_departure_then_arriving_in_time_then_the_buffers():
    window = (at(8), at(9))
    on_time = conn(at(8, 10), at(8, 50))
    early = conn(at(8, 6), at(8, 45))
    assert transit.pick(on_time, early, window, BUFFER, NOW) is on_time
    late = conn(at(8, 20), at(8, 58))
    assert transit.pick(late, early, window, BUFFER, NOW) is early
    # Neither keeps both buffers: leaving into the buffer beats arriving into it.
    tight = conn(at(8, 2), at(8, 40))
    assert transit.pick(late, tight, window, BUFFER, NOW) is tight
    assert transit.pick(late, None, window, BUFFER, NOW) is late
    too_late = conn(at(8, 30), at(9, 10))
    too_early = conn(at(7, 50), at(8, 30))
    assert transit.pick(too_late, too_early, window, BUFFER, NOW) is None
    assert transit.pick(conn(at(6, 50), at(7, 30)), None, (None, None), BUFFER, NOW) is None


@pytest.fixture
def connections(monkeypatch):
    table: dict[tuple[str, datetime], TransitConnection | None] = {}
    asked = []

    async def lookup(origin, destination, *, depart_at=None, arrive_by=None):
        key = ("arrive", arrive_by) if arrive_by else ("depart", depart_at)
        asked.append(key)
        return table.get(key)

    monkeypatch.setattr(transit, "lookup", lookup)
    return SimpleNamespace(table=table, asked=asked)


@pytest.mark.asyncio
async def test_fit_writes_the_real_connection_and_names_it(connections):
    modelled = leg(at(8, 5), at(8, 40), note="Bike stays at Uni")
    connections.table[("depart", at(8, 5))] = conn(at(8, 7), at(8, 41))

    [fitted] = await transit.fit_legs([modelled], [(at(7), at(8)), (at(9), at(10))], now=NOW, buffer=BUFFER)

    assert (fitted.depart, fitted.arrive) == (at(8, 7), at(8, 41))
    assert fitted.depart.tzinfo == TZ
    assert fitted.note == "S1 from Hauptbahnhof at 08:11\nBike stays at Uni"


@pytest.mark.asyncio
async def test_fit_falls_back_to_arriving_in_time(connections):
    modelled = leg(at(8, 5), at(8, 40))
    connections.table[("depart", at(8, 5))] = conn(at(8, 20), at(8, 58))
    connections.table[("arrive", at(8, 55))] = conn(at(8, 6), at(8, 45))

    [fitted] = await transit.fit_legs([modelled], [(at(7), at(8)), (at(9), at(10))], now=NOW, buffer=BUFFER)

    assert (fitted.depart, fitted.arrive) == (at(8, 6), at(8, 45))


@pytest.mark.asyncio
async def test_fit_keeps_modelled_times_when_nothing_fits_or_nothing_is_asked(connections):
    tight = leg(at(8, 5), at(8, 40))
    connections.table[("depart", at(8, 5))] = conn(at(8, 30), at(9, 10))
    bike = leg(at(10, 5), at(10, 30), mode="bicycling")
    far = leg(at(8, 5) + timedelta(days=2), at(8, 40) + timedelta(days=2))
    past = leg(at(6), at(6, 30))
    legs = [tight, bike, far, past]

    assert await transit.fit_legs(legs, [(at(7), at(8)), (at(9), at(10))], now=NOW, buffer=BUFFER) == legs
    assert connections.asked == [("depart", at(8, 5)), ("arrive", at(8, 55))]


@pytest.mark.asyncio
async def test_fit_skips_a_leg_in_conflict_and_keeps_legs_apart(connections):
    clash = leg(at(8, 5), at(8, 40))
    assert await transit.fit_legs([clash], [(at(8, 30), at(9))], now=NOW, buffer=BUFFER) == [clash]
    assert connections.asked == []

    # Back to back with no event between: the first leg's connection would
    # run into the second, so only the second moves.
    there = leg(at(8, 5), at(8, 40))
    back = PlannedLeg("lec", "home", "Uni", "Home", "transit", at(8, 45), at(9, 20))
    connections.table[("depart", at(8, 5))] = conn(at(8, 10), at(8, 50))
    connections.table[("depart", at(8, 45))] = conn(at(8, 50), at(9, 25))
    fitted = await transit.fit_legs([back, there], [], now=NOW, buffer=BUFFER)
    assert fitted[1] == there
    assert (fitted[0].depart, fitted[0].arrive) == (at(8, 50), at(9, 25))


@pytest.fixture
def fresh_lookup(monkeypatch):
    monkeypatch.setattr(transit, "_cache", {})
    monkeypatch.setattr(transit, "_backoff_until", 0.0)


@pytest.mark.asyncio
async def test_lookup_caches_and_backs_off_after_a_failure(monkeypatch, fresh_lookup):
    calls = []

    async def ok(**kw):
        calls.append(kw)
        return conn(at(8, 7), at(8, 41))

    monkeypatch.setattr(maps, "transit_connection", ok)
    first = await transit.lookup("Home", "Uni", depart_at=at(8, 5))
    again = await transit.lookup("Home", "Uni", depart_at=at(8, 5))
    assert first == again and len(calls) == 1

    async def denied(**kw):
        calls.append(kw)
        raise MapsLookupError("routes status=403")

    monkeypatch.setattr(maps, "transit_connection", denied)
    assert await transit.lookup("Home", "Uni", arrive_by=at(9)) is None
    assert await transit.lookup("Home", "Library", arrive_by=at(9)) is None
    assert len(calls) == 2
    # Cached answers still come back during the pause.
    assert await transit.lookup("Home", "Uni", depart_at=at(8, 5)) == first
