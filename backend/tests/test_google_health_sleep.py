from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.health import client as health_client  # noqa: E402
from app.services.health import sleep as sleep_service  # noqa: E402


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sleep_payload(*sessions: tuple[datetime, datetime, str]) -> dict:
    return {
        "dataPoints": [
            {
                "sleep": {
                    "interval": {"startTime": _rfc3339(start), "endTime": _rfc3339(end)},
                    "type": sleep_type,
                }
            }
            for start, end, sleep_type in sessions
        ]
    }


@pytest.mark.asyncio
async def test_google_health_client_list_sleep_request(monkeypatch):
    calls = []

    class FakeResponse:
        is_error = False

        def raise_for_status(self):
            return None

        def json(self):
            return {"dataPoints": []}

    class FakeAsyncClient:
        def __init__(self, *, timeout, headers):
            self.timeout = timeout
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params):
            calls.append(
                {
                    "url": url,
                    "params": params,
                    "headers": self.headers,
                    "timeout": self.timeout,
                }
            )
            return FakeResponse()

    monkeypatch.setattr(health_client.httpx, "AsyncClient", FakeAsyncClient)
    client = health_client.GoogleHealthClient("access-token", timeout=12.0)
    start = datetime(2026, 7, 4, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    end = datetime(2026, 7, 5, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    payload = await client.list_sleep(start=start, end=end)

    assert payload == {"dataPoints": []}
    assert calls == [
        {
            "url": "https://health.googleapis.com/v4/users/me/dataTypes/sleep/dataPoints",
            "params": {
                "filter": (
                    f'sleep.interval.end_time >= "{_rfc3339(start)}" AND '
                    f'sleep.interval.end_time < "{_rfc3339(end)}"'
                ),
                "pageSize": 25,
            },
            "headers": {"Authorization": "Bearer access-token"},
            "timeout": 12.0,
        }
    ]


@pytest.mark.asyncio
async def test_list_sleep_logs_google_error_body_before_raising(monkeypatch, caplog):
    body = '{"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "denied"}}'

    class FakeAsyncClient:
        def __init__(self, *, timeout, headers):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, params):
            return httpx.Response(403, request=httpx.Request("GET", url), text=body)

    monkeypatch.setattr(health_client.httpx, "AsyncClient", FakeAsyncClient)
    client = health_client.GoogleHealthClient("access-token")

    with caplog.at_level(logging.WARNING, logger="app.services.health.client"):
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_sleep(
                start=datetime(2026, 7, 4, tzinfo=timezone.utc),
                end=datetime(2026, 7, 5, tzinfo=timezone.utc),
            )

    assert "403" in caplog.text
    assert "PERMISSION_DENIED" in caplog.text


@pytest.mark.asyncio
async def test_list_sleep_rejects_naive_or_empty_range():
    client = health_client.GoogleHealthClient("access-token")
    aware = datetime(2026, 7, 4, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        await client.list_sleep(start=datetime(2026, 7, 4), end=aware)
    with pytest.raises(ValueError):
        await client.list_sleep(start=aware, end=aware)


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
        async def list_sleep(self, *, start, end):
            calls.append((start, end))
            return _sleep_payload(
                (second_start, second_end, "STAGES"),
                (first_start, first_end, "CLASSIC"),
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
    assert [segment.sleep_type for segment in interval.segments] == ["CLASSIC", "STAGES"]
    assert [segment.start for segment in interval.segments] == [
        first_start.astimezone(timezone.utc),
        second_start.astimezone(timezone.utc),
    ]


@pytest.mark.asyncio
async def test_todays_sleep_interval_returns_none_when_google_has_no_data(monkeypatch):
    class FakeClient:
        async def list_sleep(self, *, start, end):
            return {"dataPoints": []}

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


def test_normalize_sleep_interval_ignores_malformed_data_points():
    tz = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 7, 4, 1, tzinfo=tz)
    end = datetime(2026, 7, 4, 6, tzinfo=tz)
    payload = _sleep_payload((start, end, "STAGES"))
    payload["dataPoints"].extend(
        [
            {"dataSource": {"name": "steps"}},  # no "sleep"
            {"sleep": {"type": "STAGES"}},  # no "interval"
            {"sleep": {"interval": {"startTime": "nonsense", "endTime": "also-bad"}}},
        ]
    )

    interval = sleep_service.normalize_sleep_interval(payload)

    assert interval is not None
    assert interval.start == start.astimezone(timezone.utc)
    assert interval.end == end.astimezone(timezone.utc)
    assert len(interval.segments) == 1


def test_parse_rfc3339_trims_nanosecond_precision():
    parsed = sleep_service._parse_rfc3339("2026-07-04T07:30:12.045123456Z")

    assert parsed == datetime(2026, 7, 4, 7, 30, 12, 45123, tzinfo=timezone.utc)
