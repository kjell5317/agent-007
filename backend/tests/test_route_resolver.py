from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.db.models.route_cache import RouteCache  # noqa: E402
from app.services.commute import resolver  # noqa: E402
from app.services.location import (  # noqa: E402
    is_online_location,
    resolve_google_maps_url,
)


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    RouteCache.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _seed(
    session,
    *,
    mode: str,
    hour_bucket: int,
    duration: int,
    origin: str = "Home",
    destination: str = "Gym",
    age_days: int = 0,
):
    row = RouteCache(
        origin=origin,
        destination=destination,
        mode=mode,
        hour_bucket=hour_bucket,
        duration_seconds=duration,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        updated_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    session.add(row)
    session.commit()
    return row


@pytest.mark.asyncio
async def test_bike_uses_single_bucket_regardless_of_hour(monkeypatch):
    session = _session()
    _seed(session, mode="bicycling", hour_bucket=0, duration=600)

    async def boom(**_kwargs):
        raise AssertionError("cache hit expected — no API call")

    monkeypatch.setattr(resolver, "distance", boom)

    morning = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 7, 9, 19, 0, tzinfo=timezone.utc)
    for departure in (morning, evening, None):
        result = await resolver.resolve_duration(
            session, origin="Home", destination="Gym",
            mode="bicycling", departure=departure,
        )
        assert result == 600


@pytest.mark.asyncio
async def test_bike_reuses_reversed_cached_route_without_maps(monkeypatch):
    session = _session()
    _seed(session, mode="bicycling", hour_bucket=0, duration=700, origin="Home", destination="Gym")

    async def boom(**_kwargs):
        raise AssertionError("reverse bike cache hit expected — no API call")

    monkeypatch.setattr(resolver, "distance", boom)

    result = await resolver.resolve_duration(
        session,
        origin="Gym",
        destination="Home",
        mode="bicycling",
        departure=datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc),
    )
    assert result == 700


@pytest.mark.asyncio
async def test_bike_prefers_exact_cached_route_over_reversed(monkeypatch):
    session = _session()
    _seed(session, mode="bicycling", hour_bucket=0, duration=700, origin="Home", destination="Gym")
    _seed(session, mode="bicycling", hour_bucket=0, duration=500, origin="Gym", destination="Home")

    async def boom(**_kwargs):
        raise AssertionError("exact bike cache hit expected — no API call")

    monkeypatch.setattr(resolver, "distance", boom)

    result = await resolver.resolve_duration(
        session,
        origin="Gym",
        destination="Home",
        mode="bicycling",
        departure=datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc),
    )
    assert result == 500


@pytest.mark.asyncio
async def test_transit_does_not_reuse_reversed_cached_route(monkeypatch):
    session = _session()
    departure = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    bucket = resolver._hour_bucket(departure, "transit")
    _seed(session, mode="transit", hour_bucket=bucket, duration=1200, origin="Home", destination="Gym")
    calls = []

    async def fresh(**kwargs):
        calls.append(kwargs)
        return 1500, 5000

    monkeypatch.setattr(resolver, "distance", fresh)
    monkeypatch.setattr(resolver.route_cache, "upsert", lambda *_args, **_kwargs: None)

    result = await resolver.resolve_duration(
        session, origin="Gym", destination="Home", mode="transit", departure=departure,
    )
    assert result == 1500
    assert calls == [
        {
            "origin": "Gym",
            "destination": "Home",
            "mode": "transit",
            "departure": departure,
        }
    ]


@pytest.mark.asyncio
async def test_fresh_transit_entry_is_served_from_cache(monkeypatch):
    session = _session()
    departure = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    bucket = resolver._hour_bucket(departure, "transit")
    _seed(session, mode="transit", hour_bucket=bucket, duration=1200, age_days=1)

    async def boom(**_kwargs):
        raise AssertionError("cache hit expected — no API call")

    monkeypatch.setattr(resolver, "distance", boom)

    result = await resolver.resolve_duration(
        session, origin="Home", destination="Gym", mode="transit", departure=departure,
    )
    assert result == 1200


@pytest.mark.asyncio
async def test_stale_transit_entry_is_refetched(monkeypatch):
    session = _session()
    departure = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    bucket = resolver._hour_bucket(departure, "transit")
    _seed(session, mode="transit", hour_bucket=bucket, duration=1200, age_days=90)

    async def fresh(**_kwargs):
        return 1500, 5000

    monkeypatch.setattr(resolver, "distance", fresh)

    result = await resolver.resolve_duration(
        session, origin="Home", destination="Gym", mode="transit", departure=departure,
    )
    assert result == 1500  # re-fetched and refreshed


@pytest.mark.asyncio
async def test_stale_transit_survives_transient_api_failure(monkeypatch):
    session = _session()
    departure = datetime(2026, 7, 6, 8, 0, tzinfo=timezone.utc)
    bucket = resolver._hour_bucket(departure, "transit")
    _seed(session, mode="transit", hour_bucket=bucket, duration=1200, age_days=90)

    async def transient(**_kwargs):
        raise resolver.MapsLookupError("rate limited", cacheable=False)

    monkeypatch.setattr(resolver, "distance", transient)

    result = await resolver.resolve_duration(
        session, origin="Home", destination="Gym", mode="transit", departure=departure,
    )
    assert result == 1200  # stale beats nothing


def test_online_location_detection():
    online = [
        None,
        "",
        "https://zoom.us/j/123",
        "http://example.com/meet",
        "meet.google.com/abc-defg",
        "Microsoft Teams Meeting",
        "Zoom",
        "Online",
        "remote",
        "Video call with Alex",
        "Webinar: Q3 planning",
    ]
    physical = [
        "Gymstreet 5, Munich",
        "https://www.google.com/maps/place/TUM+Campus/@48.26566,11.66256,17z",
        "https://maps.google.com/?q=Marienplatz+8,+Munich",
        "https://maps.app.goo.gl/abcdef",
        "https://goo.gl/maps/abcdef",
        "Onlinerstrasse 2",  # word boundary — no false positive
        "Cafe Remoteweg 3",
        "Marienplatz 8",
    ]
    for loc in online:
        assert is_online_location(loc), loc
    for loc in physical:
        assert not is_online_location(loc), loc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.google.com/maps/place/TUM+Campus/@48.26566,11.66256,17z",
            "48.26566,11.66256",
        ),
        (
            "https://maps.google.com/?q=Marienplatz+8,+Munich",
            "Marienplatz 8, Munich",
        ),
        (
            "https://www.google.com/maps/search/?api=1&query=48.137154,11.576124",
            "48.137154,11.576124",
        ),
        (
            "https://www.google.com/maps/dir/?api=1&destination=Olympiapark+Munich",
            "Olympiapark Munich",
        ),
        (
            "https://www.google.com/maps/place/English+Garden",
            "English Garden",
        ),
    ],
)
async def test_google_maps_url_resolves_direct_shapes(url, expected):
    assert await resolve_google_maps_url(url) == expected


@pytest.mark.asyncio
async def test_google_maps_short_link_uses_redirected_url(monkeypatch):
    class Response:
        url = "https://www.google.com/maps/place/TUM+Campus/@48.26566,11.66256,17z"

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            assert url == "https://maps.app.goo.gl/abcdef"
            assert self.kwargs["follow_redirects"] is True
            return Response()

    monkeypatch.setattr("app.services.location.httpx.AsyncClient", Client)

    assert await resolve_google_maps_url("https://maps.app.goo.gl/abcdef") == "48.26566,11.66256"
