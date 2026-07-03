from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.db.models.route_cache import RouteCache  # noqa: E402
from app.services.commute import resolver  # noqa: E402
from app.services.location import is_online_location  # noqa: E402


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    RouteCache.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _seed(session, *, mode: str, hour_bucket: int, duration: int, age_days: int = 0):
    row = RouteCache(
        origin="Home",
        destination="Gym",
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
        "Onlinerstrasse 2",  # word boundary — no false positive
        "Cafe Remoteweg 3",
        "Marienplatz 8",
    ]
    for loc in online:
        assert is_online_location(loc), loc
    for loc in physical:
        assert not is_online_location(loc), loc
