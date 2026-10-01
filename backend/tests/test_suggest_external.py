from types import SimpleNamespace

import pytest

from app.api import search as search_api
from app.db.schemas.search import SearchHit


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
