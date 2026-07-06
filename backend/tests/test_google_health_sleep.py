from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.health import client as health_client  # noqa: E402
from app.services.health import sleep as sleep_service  # noqa: E402


def _nanos(dt: datetime) -> str:
    utc = dt.astimezone(timezone.utc)
    return str(int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1000)


def _millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _sleep_payload(*segments: tuple[datetime, datetime, int]) -> dict:
    return {
        "bucket": [
            {
                "dataset": [
                    {
                        "point": [
                            {
                                "dataTypeName": "com.google.sleep.segment",
                                "startTimeNanos": _nanos(start),
                                "endTimeNanos": _nanos(end),
                                "value": [{"intVal": stage}],
                            }
                            for start, end, stage in segments
                        ]
                    }
                ]
            }
        ]
    }


@pytest.mark.asyncio
async def test_google_fit_client_aggregate_sleep_request(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"bucket": []}

    class FakeAsyncClient:
        def __init__(self, *, timeout, headers):
            self.timeout = timeout
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json):
            calls.append(
                {
                    "url": url,
                    "json": json,
                    "headers": self.headers,
                    "timeout": self.timeout,
                }
            )
            return FakeResponse()

    monkeypatch.setattr(health_client.httpx, "AsyncClient", FakeAsyncClient)
    client = health_client.GoogleFitClient("access-token", timeout=12.0)
    start = datetime(2026, 7, 4, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    end = datetime(2026, 7, 5, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    payload = await client.aggregate_sleep_segments(start=start, end=end)

    assert payload == {"bucket": []}
    assert calls == [
        {
            "url": "https://www.googleapis.com/fitness/v1/users/me/dataset:aggregate",
            "json": {
                "aggregateBy": [{"dataTypeName": "com.google.sleep.segment"}],
                "startTimeMillis": _millis(start),
                "endTimeMillis": _millis(end),
            },
            "headers": {"Authorization": "Bearer access-token"},
            "timeout": 12.0,
        }
    ]


@pytest.mark.asyncio
async def test_todays_sleep_interval_uses_local_day_bounds(monkeypatch):
    tz = ZoneInfo("Europe/Berlin")
    calls = []
    session = object()

    first_start = datetime(2026, 7, 4, 1, 0, tzinfo=tz)
    first_end = datetime(2026, 7, 4, 4, 0, tzinfo=tz)
    second_start = datetime(2026, 7, 4, 4, 15, tzinfo=tz)
    second_end = datetime(2026, 7, 4, 7, 30, tzinfo=tz)

    class FakeClient:
        async def aggregate_sleep_segments(self, *, start, end):
            calls.append((start, end))
            return _sleep_payload(
                (second_start, second_end, 110),
                (first_start, first_end, 109),
            )

    async def fake_authorized_client(session_arg, account_key):
        assert session_arg is session
        assert account_key == "user@example.com"
        return FakeClient()

    monkeypatch.setattr(sleep_service, "user_tz", lambda: tz)
    monkeypatch.setattr(sleep_service, "authorized_client", fake_authorized_client)

    interval = await sleep_service.request_todays_sleep_interval(
        session,
        account_key="user@example.com",
        now=datetime(2026, 7, 3, 22, 30, tzinfo=timezone.utc),
    )

    assert calls == [
        (
            datetime(2026, 7, 4, 0, 0, tzinfo=tz),
            datetime(2026, 7, 5, 0, 0, tzinfo=tz),
        )
    ]
    assert interval is not None
    assert interval.start == first_start.astimezone(timezone.utc)
    assert interval.end == second_end.astimezone(timezone.utc)
    assert [segment.sleep_stage for segment in interval.segments] == [109, 110]
    assert [segment.start for segment in interval.segments] == [
        first_start.astimezone(timezone.utc),
        second_start.astimezone(timezone.utc),
    ]


@pytest.mark.asyncio
async def test_todays_sleep_interval_returns_none_when_google_has_no_data(monkeypatch):
    class FakeClient:
        async def aggregate_sleep_segments(self, *, start, end):
            return {"bucket": [{"dataset": [{"point": []}]}]}

    async def fake_authorized_client(session, account_key):
        return FakeClient()

    monkeypatch.setattr(sleep_service, "user_tz", lambda: ZoneInfo("Europe/Berlin"))
    monkeypatch.setattr(sleep_service, "authorized_client", fake_authorized_client)

    interval = await sleep_service.request_todays_sleep_interval(
        SimpleNamespace(),
        now=datetime(2026, 7, 4, 10, tzinfo=timezone.utc),
    )

    assert interval is None


@pytest.mark.asyncio
async def test_request_awake_minutes_diffs_now_from_sleep_end(monkeypatch, caplog):
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 7, 4, 9, 0, tzinfo=tz)
    sleep_start = datetime(2026, 7, 4, 1, 0, tzinfo=tz)
    sleep_end = datetime(2026, 7, 4, 7, 30, tzinfo=tz)

    async def fake_interval(session, *, account_key, now):
        return SimpleNamespace(
            start=sleep_start.astimezone(timezone.utc),
            end=sleep_end.astimezone(timezone.utc),
            segments=[],
        )

    monkeypatch.setattr(sleep_service, "request_todays_sleep_interval", fake_interval)

    with caplog.at_level(logging.INFO, logger="app.services.health.sleep"):
        minutes = await sleep_service.request_awake_minutes(SimpleNamespace(), now=now)

    assert minutes == 90
    assert "google sleep · start=" in caplog.text
    assert "segments=0" in caplog.text
    assert "awake_minutes=90" in caplog.text


@pytest.mark.asyncio
async def test_request_awake_minutes_is_zero_without_sleep(monkeypatch, caplog):
    async def fake_interval(session, *, account_key, now):
        return None

    monkeypatch.setattr(sleep_service, "request_todays_sleep_interval", fake_interval)

    with caplog.at_level(logging.INFO, logger="app.services.health.sleep"):
        minutes = await sleep_service.request_awake_minutes(
            SimpleNamespace(), now=datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc)
        )

    assert minutes == 0
    assert "google sleep · none returned" in caplog.text


def test_normalize_sleep_interval_ignores_non_sleep_and_malformed_points():
    tz = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 7, 4, 1, tzinfo=tz)
    end = datetime(2026, 7, 4, 6, tzinfo=tz)
    payload = _sleep_payload((start, end, 109))
    payload["bucket"][0]["dataset"][0]["point"].extend(
        [
            {
                "dataTypeName": "com.google.step_count.delta",
                "startTimeNanos": _nanos(start),
                "endTimeNanos": _nanos(end),
            },
            {"dataTypeName": "com.google.sleep.segment"},
        ]
    )

    interval = sleep_service.normalize_sleep_interval(payload)

    assert interval is not None
    assert interval.start == start.astimezone(timezone.utc)
    assert interval.end == end.astimezone(timezone.utc)
    assert len(interval.segments) == 1
