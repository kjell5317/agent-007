from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.db.clients import documents as documents_store  # noqa: E402
from app.db.clients import search as search_client  # noqa: E402
from app.services.calendar import cache as cal_cache  # noqa: E402
from app.services.calendar.client import normalize  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _item(event_id: str, start: datetime, *, series: str | None = None, summary: str = "Standup") -> dict:
    item = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    if series:
        item["recurringEventId"] = series
    return item


def _instance(days: int) -> dict:
    start = NOW + timedelta(days=days)
    return _item(f"abc_{start:%Y%m%dT%H%M%SZ}", start, series="abc")


@pytest.fixture
def store(monkeypatch):
    rows: dict[str, dict] = {}
    deleted: list[str] = []
    embedded: list[str] = []

    def upsert(_session, **kw):
        rows[kw["external_id"]] = kw

    def delete(_session, *, provider, external_id):
        deleted.append(external_id)
        return rows.pop(external_id, None) is not None

    async def embed(text):
        embedded.append(text)
        return [0.1, 0.2]

    monkeypatch.setattr(documents_store, "get_by_external_id", lambda _s, **kw: None)
    monkeypatch.setattr(documents_store, "upsert", upsert)
    monkeypatch.setattr(documents_store, "delete", delete)
    monkeypatch.setattr(cal_cache, "embed", embed)
    monkeypatch.setattr("app.services.calendar.attributes.cached_attributes", lambda *a, **k: {})
    monkeypatch.setattr("app.services.calendar.attributes.label_catalog", lambda _s: ({}, {}))
    return type("Store", (), {"rows": rows, "deleted": deleted, "embedded": embedded})


@pytest.mark.asyncio
async def test_far_instances_are_skipped_and_evicted_but_far_one_offs_stay(store):
    near = [normalize(_instance(d), "primary") for d in (7, 14, 21)]
    far = normalize(_instance(400), "primary")
    conference = normalize(_item("conf", NOW + timedelta(days=500), summary="Conference"), "primary")

    summary = await cal_cache.cache_calendar_events(None, active=[*near, far, conference])

    assert set(store.rows) == {f"primary:{e.id}" for e in [*near, conference]}
    assert f"primary:{far.id}" in store.deleted
    assert summary["upserted"] == 4
    # Every instance of the series has the same text: one embedding call.
    assert summary["embedded"] == 2
    assert len(store.embedded) == 2


class _Client:
    def __init__(self, items: list[dict]):
        self.items = items
        self.asked: list[tuple] = []

    async def list_events(self, calendar_id, *, time_min=None, time_max=None):
        self.asked.append((calendar_id, time_min, time_max))
        return self.items


@pytest.mark.asyncio
async def test_refresh_adds_only_uncached_recurring_instances(store, monkeypatch):
    cached = _instance(7)
    new = _instance(300)
    one_off = _item("lunch", NOW + timedelta(days=10), summary="Lunch")
    cancelled = {**_instance(14), "status": "cancelled"}
    client = _Client([cached, new, one_off, cancelled])
    monkeypatch.setattr(
        documents_store, "calendar_external_ids", lambda _s, **kw: {f"primary:{cached['id']}"}
    )

    added = await cal_cache.refresh_recurring(None, client, ["primary"], now=NOW)

    assert added == 1
    assert set(store.rows) == {f"primary:{new['id']}"}
    assert client.asked == [("primary", NOW, NOW + cal_cache.RECURRING_HORIZON)]


def _suggest_sql(**kw) -> str:
    captured = {}

    class Session:
        def execute(self, sql, params):
            captured["sql"] = str(sql)
            return type("Rows", (), {"all": lambda self: []})()

    search_client.suggest(Session(), tsquery="standup:*", half_life_days=100.0, limit=10, **kw)
    return captured["sql"]


def test_calendar_suggestions_show_one_hit_per_series():
    sql = _suggest_sql(branches=frozenset({"document"}), source="calendar")
    assert "DISTINCT ON (CASE WHEN s.external_id ~ '_[0-9]{8}(T[0-9]{6}Z)?$'" in sql
    assert "'series:' || lower(s.title)" in sql
    mixed = _suggest_sql(branches=frozenset({"task", "document"}))
    assert "DISTINCT ON" not in mixed
