from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.api import search as search_api
from app.db.schemas.search import SearchHit
from app.services.calendar.client import normalize
from app.services.search import calendar as live_calendar
from app.services.search import contacts


@pytest.mark.asyncio
async def test_calendar_external_suggestions_ask_google_calendar_only(monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    calls = []

    async def fake_calendar(_session, query, *, k, timeout):
        calls.append((query, k, timeout))
        return [SearchHit(type="document", id="evt-1", title="Dentist", source="calendar", score=0)]

    monkeypatch.setattr(search_api, "SessionLocal", Session)
    monkeypatch.setattr(search_api, "search_calendar_live", fake_calendar)
    monkeypatch.setattr(search_api, "get_settings", lambda: SimpleNamespace(search_calendar_timeout_seconds=3.0))

    result = await search_api.suggest_external("Dentist", limit=10, kind="calendar")

    assert [hit.id for hit in result.hits] == ["evt-1"]
    assert calls == [("Dentist", 10, 3.0)]


def _event(event_id: str, start: datetime, summary: str, series: str | None = None):
    item = {
        "id": event_id, "summary": summary, "htmlLink": f"https://calendar/{event_id}",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    if series:
        item["recurringEventId"] = series
    return normalize(item, "me@example.com")


def test_live_calendar_shows_the_next_instance_of_each_series():
    now = datetime(2026, 11, 17, 9, tzinfo=timezone.utc)
    standups = [_event(f"s_{d}", now + timedelta(days=d), "Standup", series="s") for d in (3, 1, 2)]
    dentist = _event("d", now + timedelta(days=2), "Dentist")

    kept = live_calendar.next_per_series([*standups, dentist])

    assert [e.id for e in kept] == ["s_1", "d"]


@pytest.mark.asyncio
async def test_live_calendar_search_is_best_effort(monkeypatch):
    settings = SimpleNamespace(google_busy_calendar_ids=["007", "me@example.com"], google_calendar_id="007")
    monkeypatch.setattr(live_calendar, "get_settings", lambda: settings)
    asked = []

    class Client:
        async def search_events(self, calendar_id, query, *, time_min, limit):
            asked.append(calendar_id)
            return [{
                "id": "evt", "summary": "Dentist", "htmlLink": "https://calendar/evt",
                "start": {"dateTime": "2026-11-20T09:00:00+00:00"},
                "end": {"dateTime": "2026-11-20T10:00:00+00:00"},
            }]

    async def client(_session, _account):
        return Client()

    monkeypatch.setattr(live_calendar, "authorized_client", client)
    hits = await live_calendar.search_calendar_live(object(), "dentist", k=5, timeout=1)
    assert asked == ["me@example.com"]
    assert [(h.id, h.url, h.source) for h in hits] == [("evt", "https://calendar/evt", "calendar")]

    async def no_grant(_session, _account):
        raise RuntimeError("No Google account connected")

    monkeypatch.setattr(live_calendar, "authorized_client", no_grant)
    assert await live_calendar.search_calendar_live(object(), "dentist", k=5, timeout=1) == []
    settings.google_busy_calendar_ids = []
    assert await live_calendar.search_calendar_live(object(), "dentist", k=5, timeout=1) == []


@pytest.mark.asyncio
async def test_github_external_suggestions_use_github_only(monkeypatch):
    calls = []

    async def fake_github(query, *, limit):
        calls.append((query, limit))
        return [SearchHit(type="github", id="acme/widgets#1", title="Fix", score=0)]

    monkeypatch.setattr(search_api, "search_github_hits", fake_github)
    result = await search_api.suggest_external("Fix", limit=10, kind="github")
    assert [hit.id for hit in result.hits] == ["acme/widgets#1"]
    assert calls == [("Fix", 10)]


@pytest.mark.asyncio
async def test_contact_search_releases_token_connection_before_network_call(monkeypatch):
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0,
                           pool_timeout=0.1)

    async def fake_token(session):
        session.execute(text("SELECT 1"))
        return SimpleNamespace(access_token="test")

    class FakeContactsClient:
        def __init__(self, _token, *, timeout):
            pass

        async def search(self, _query, *, limit):
            with Session(engine) as other:
                assert other.execute(text("SELECT 1")).scalar() == 1
            return []

    monkeypatch.setattr(contacts, "get_fresh_google_token", fake_token)
    monkeypatch.setattr(contacts, "ContactsClient", FakeContactsClient)

    try:
        with Session(engine) as session:
            assert await contacts.search_contacts(session, "Kjell", k=3, timeout=1) == []
    finally:
        engine.dispose()
