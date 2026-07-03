from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.api import tasks as tasks_api  # noqa: E402
from app.db.clients import route_cache as route_cache_store  # noqa: E402
from app.db.models.route_cache import RouteCache  # noqa: E402


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    RouteCache.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _seed(session, origin: str, destination: str):
    session.add(
        RouteCache(
            origin=origin,
            destination=destination,
            mode="bicycling",
            hour_bucket=0,
            duration_seconds=600,
            distance_meters=None,
        )
    )
    session.commit()


def test_location_suggestions_match_origins_and_destinations():
    session = _session()
    _seed(session, "Client Site", "Office")
    _seed(session, "Home", "Gym")

    assert route_cache_store.location_suggestions(session, query="client") == [
        "Client Site"
    ]
    assert route_cache_store.location_suggestions(session, query="off") == ["Office"]


def test_location_suggestions_deduplicate_origins_and_destinations():
    session = _session()
    _seed(session, "Home", "Office")
    _seed(session, "Gym", "Home")

    assert route_cache_store.location_suggestions(session, query="home") == ["Home"]


def test_location_suggestions_trim_query_and_support_empty_query():
    session = _session()
    _seed(session, "Home", "Cafe Central")
    _seed(session, "Office", "Gym")

    assert route_cache_store.location_suggestions(session, query="  cafe  ") == [
        "Cafe Central"
    ]
    assert route_cache_store.location_suggestions(session, query="") == [
        "Cafe Central",
        "Gym",
        "Home",
    ]


def test_location_suggestions_limit_results_to_three():
    session = _session()
    _seed(session, "Alpha", "Bravo")
    _seed(session, "Charlie", "Delta")

    assert route_cache_store.location_suggestions(session, query="") == [
        "Alpha",
        "Bravo",
        "Charlie",
    ]


@pytest.mark.asyncio
async def test_location_suggestions_endpoint_returns_payload(monkeypatch):
    calls = []
    session = object()

    def fake_location_suggestions(_session, *, query, limit):
        calls.append((_session, query, limit))
        return ["Gym", "Office"]

    monkeypatch.setattr(
        tasks_api.route_cache_store,
        "location_suggestions",
        fake_location_suggestions,
    )

    result = await tasks_api.location_suggestions(q=" gym ", session=session)

    assert calls == [(session, " gym ", 3)]
    assert result.suggestions == ["Gym", "Office"]
