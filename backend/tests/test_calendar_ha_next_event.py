from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.calendar import discover as discover_mod  # noqa: E402
from app.services import home_assistant as ha  # noqa: E402


BERLIN = ZoneInfo("Europe/Berlin")


def _event(
    event_id: str,
    start: datetime,
    *,
    all_day: bool = False,
    transparency: str | None = None,
) -> CalendarEvent:
    raw = {}
    if transparency is not None:
        raw["transparency"] = transparency
    return CalendarEvent(
        id=event_id,
        calendar_id="primary",
        summary=event_id,
        description=None,
        start=start,
        end=start + timedelta(hours=1),
        all_day=all_day,
        location=None,
        html_link=None,
        private_properties={},
        raw=raw,
    )


def _raw_event(event_id: str, start: datetime, *, transparency: str | None = None) -> dict:
    raw = {
        "id": event_id,
        "summary": event_id,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat()},
    }
    if transparency is not None:
        raw["transparency"] = transparency
    return raw


def test_ha_picker_keeps_todays_first_event_before_it_starts():
    now = datetime(2026, 6, 6, 0, 1, tzinfo=BERLIN)
    today_first = datetime(2026, 6, 6, 6, 0, tzinfo=BERLIN)
    today_later = datetime(2026, 6, 6, 9, 0, tzinfo=BERLIN)
    tomorrow = datetime(2026, 6, 7, 7, 0, tzinfo=BERLIN)

    selected = discover_mod._select_home_assistant_next_event(
        [
            _event("later", today_later),
            _event("tomorrow", tomorrow),
            _event("first", today_first),
        ],
        now=now,
        tz=BERLIN,
    )

    assert selected == today_first
    assert discover_mod._home_assistant_datetime(selected, tz=BERLIN) == "2026-06-06 06:00:00"


def test_ha_picker_switches_to_next_local_day_after_todays_first_event_arrives():
    now = datetime(2026, 6, 6, 6, 1, tzinfo=BERLIN)
    today_first = datetime(2026, 6, 6, 6, 0, tzinfo=BERLIN)
    today_later = datetime(2026, 6, 6, 9, 0, tzinfo=BERLIN)
    tomorrow_first = datetime(2026, 6, 7, 7, 0, tzinfo=BERLIN)

    selected = discover_mod._select_home_assistant_next_event(
        [
            _event("today-first", today_first),
            _event("later-today", today_later),
            _event("tomorrow", tomorrow_first),
        ],
        now=now,
        tz=BERLIN,
    )

    assert selected == tomorrow_first


def test_ha_picker_ignores_all_day_transparent_and_previous_day_starts():
    now = datetime(2026, 6, 6, 1, 0, tzinfo=BERLIN)
    previous_day = datetime(2026, 6, 5, 23, 30, tzinfo=BERLIN)
    all_day = datetime(2026, 6, 6, 0, 0, tzinfo=BERLIN)
    transparent = datetime(2026, 6, 6, 5, 0, tzinfo=BERLIN)
    busy = datetime(2026, 6, 6, 6, 0, tzinfo=BERLIN)

    selected = discover_mod._select_home_assistant_next_event(
        [
            _event("previous", previous_day),
            _event("all-day", all_day, all_day=True),
            _event("free", transparent, transparency="transparent"),
            _event("busy", busy),
        ],
        now=now,
        tz=BERLIN,
    )

    assert selected == busy


def test_ha_picker_skips_to_next_event_when_tomorrow_has_none():
    now = datetime(2026, 6, 6, 8, 0, tzinfo=BERLIN)
    today_first = datetime(2026, 6, 6, 7, 0, tzinfo=BERLIN)
    later_today = datetime(2026, 6, 6, 12, 0, tzinfo=BERLIN)
    day_after_tomorrow = datetime(2026, 6, 8, 7, 0, tzinfo=BERLIN)

    selected = discover_mod._select_home_assistant_next_event(
        [
            _event("today-first", today_first),
            _event("later-today", later_today),
            _event("next", day_after_tomorrow),
        ],
        now=now,
        tz=BERLIN,
    )

    assert selected == day_after_tomorrow


@pytest.mark.asyncio
async def test_discovery_updates_ha_next_event_even_without_changed_events(monkeypatch):
    now_local = datetime.now(BERLIN)
    free_today = now_local.replace(hour=5, minute=0, second=0, microsecond=0)
    if free_today <= now_local:
        free_today += timedelta(days=1)
    next_busy = (free_today + timedelta(days=1)).replace(hour=6, minute=0)

    await _run_discovery_with_events(
        monkeypatch,
        [_raw_event("free", free_today, transparency="transparent"), _raw_event("busy", next_busy)],
        expected_value=next_busy.strftime("%Y-%m-%d %H:%M:%S"),
    )


@pytest.mark.asyncio
async def test_discovery_does_not_fail_when_ha_call_errors(monkeypatch):
    class FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            raise RuntimeError("ha unavailable")

    monkeypatch.setattr(ha.httpx, "AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(
        ha,
        "get_settings",
        lambda: SimpleNamespace(
            home_assistant_url="http://ha",
            home_assistant_token="token",
            home_assistant_next_event_entity_id="input_datetime.007",
        ),
    )

    now_local = datetime.now(BERLIN)
    next_busy = (now_local + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)

    await _run_discovery_with_events(
        monkeypatch,
        [_raw_event("busy", next_busy)],
        expected_value=None,
        patch_setter=False,
    )


def test_parse_home_assistant_datetime_reattaches_user_tz(monkeypatch):
    monkeypatch.setattr(ha, "user_tz", lambda: BERLIN)

    parsed = ha._parse_home_assistant_datetime("2026-07-06 23:30:00")

    assert parsed == datetime(2026, 7, 6, 23, 30, tzinfo=BERLIN)


@pytest.mark.parametrize("state", [None, "", "unknown", "unavailable", "garbage"])
def test_parse_home_assistant_datetime_returns_none_for_unusable_state(monkeypatch, state):
    monkeypatch.setattr(ha, "user_tz", lambda: BERLIN)

    assert ha._parse_home_assistant_datetime(state) is None


@pytest.mark.asyncio
async def test_get_next_event_datetime_reads_state_via_get(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"entity_id": "input_datetime.007", "state": "2026-07-06 23:30:00"}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            calls.append({"url": url, "headers": headers, "timeout": self.timeout})
            return FakeResponse()

    monkeypatch.setattr(ha.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(ha, "user_tz", lambda: BERLIN)
    monkeypatch.setattr(
        ha,
        "get_settings",
        lambda: SimpleNamespace(
            home_assistant_url="http://ha/",
            home_assistant_token="token",
            home_assistant_next_event_entity_id="input_datetime.007",
        ),
    )

    value = await ha.get_next_event_datetime()

    assert value == datetime(2026, 7, 6, 23, 30, tzinfo=BERLIN)
    assert calls == [
        {
            "url": "http://ha/api/states/input_datetime.007",
            "headers": {"Authorization": "Bearer token"},
            "timeout": ha._TIMEOUT,
        }
    ]


@pytest.mark.asyncio
async def test_minutes_until_next_event_prep_counts_down_to_lead(monkeypatch):
    target = datetime(2026, 7, 7, 7, 0, tzinfo=BERLIN)

    async def fake_get():
        return target

    monkeypatch.setattr(ha, "get_next_event_datetime", fake_get)

    now = datetime(2026, 7, 6, 23, 0, tzinfo=BERLIN)
    minutes = await ha.minutes_until_next_event_prep(now=now)

    # 07:00 − 45min = 06:15; from 23:00 that's 7h15m = 435 minutes.
    assert minutes == 435


@pytest.mark.asyncio
async def test_minutes_until_next_event_prep_none_when_unavailable(monkeypatch):
    async def fake_get():
        return None

    monkeypatch.setattr(ha, "get_next_event_datetime", fake_get)

    minutes = await ha.minutes_until_next_event_prep(
        now=datetime(2026, 7, 6, 23, 0, tzinfo=timezone.utc)
    )

    assert minutes is None


async def _run_discovery_with_events(
    monkeypatch,
    raw_events: list[dict],
    *,
    expected_value: str | None,
    patch_setter: bool = True,
) -> None:
    settings = SimpleNamespace(
        google_calendar_id="primary",
        google_busy_calendar_ids=[],
        event_buffer_minutes=10,
        commute_enabled=False,
    )
    monkeypatch.setattr(discover_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(discover_mod, "user_tz", lambda: BERLIN)

    token_row = SimpleNamespace(account_key="acct", extra={})
    monkeypatch.setattr(
        discover_mod.oauth_tokens,
        "get_decrypted",
        lambda session, *, provider, account_key: token_row,
    )
    monkeypatch.setattr(
        discover_mod.oauth_tokens,
        "set_extra",
        lambda session, *, provider, account_key, patch: None,
    )

    class FakeClient:
        async def sync_events(self, cid, *, sync_token=None, time_min=None, time_max=None):
            return [], "sync-token"

        async def list_events(self, cid, *, time_min=None, time_max=None):
            return raw_events

    async def fake_authorized_client(session, account_key):
        return FakeClient()

    captured = {}

    async def fake_set_next_event_datetime(value: str):
        captured["value"] = value

    monkeypatch.setattr(discover_mod, "authorized_client", fake_authorized_client)
    monkeypatch.setattr(discover_mod, "next_event_datetime_configured", lambda: True)
    if patch_setter:
        monkeypatch.setattr(discover_mod, "set_next_event_datetime", fake_set_next_event_datetime)

    session = SimpleNamespace(commit=lambda: None)
    summary = await discover_mod.discover_updated_events(
        session, calendar_ids=["primary"], account_key="acct",
    )

    assert summary["updated"] == 0
    if expected_value is not None:
        assert captured["value"] == expected_value
