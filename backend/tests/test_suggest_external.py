from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.api import search as search_api
from app.db.schemas.search import SearchHit
from app.services.search import contacts


@pytest.mark.asyncio
async def test_external_suggestions_interleave_contacts_and_drive(monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    calls = []

    async def fake_drive(_session, query, *, k, timeout):
        calls.append(("drive", query, k, timeout))
        return [SearchHit(type="drive", id=f"file-{n}", title=f"File {n}", score=0)
                for n in range(2)]

    async def fake_contacts(_session, query, *, k, timeout):
        calls.append(("contact", query, k, timeout))
        return [SearchHit(type="contact", id=f"person-{n}", title=f"Person {n}", score=0)
                for n in range(2)]

    monkeypatch.setattr(search_api, "SessionLocal", Session)
    monkeypatch.setattr(search_api, "search_drive", fake_drive)
    monkeypatch.setattr(search_api, "search_contacts", fake_contacts)
    monkeypatch.setattr(search_api, "get_settings", lambda: SimpleNamespace(
        search_drive_timeout_seconds=4.0, search_contacts_timeout_seconds=4.0,
    ))

    result = await search_api.suggest_external("Kjell", limit=3)

    assert [hit.id for hit in result.hits] == ["person-0", "file-0", "person-1"]
    assert {name for name, *_ in calls} == {"drive", "contact"}

    calls.clear()
    contacts_only = await search_api.suggest_external("Kjell", limit=3, kind="contact")
    assert [hit.id for hit in contacts_only.hits] == ["person-0", "person-1"]
    assert [name for name, *_ in calls] == ["contact"]


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
