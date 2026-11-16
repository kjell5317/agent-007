from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.input.slack.client import (  # noqa: E402
    SlackAPIError,
    SlackAuthError,
    SlackClient,
    _raise_for,
)
from app.services.input.slack.source import SlackSource  # noqa: E402


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "messages": []})


def _patch_sleep(monkeypatch) -> list[float]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    return sleeps


@pytest.mark.asyncio
async def test_get_retries_transient_timeout(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ReadTimeout("slow slack")
        return _ok_response(request)

    slack = SlackClient("xoxp-test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await slack._get(client, "conversations.history", {"channel": "C1"})

    assert payload["ok"] is True
    assert calls == 3
    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_get_gives_up_after_max_attempts(monkeypatch):
    _patch_sleep(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow slack")

    slack = SlackClient("xoxp-test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            await slack._get(client, "conversations.history", {"channel": "C1"})

    assert calls == 3


@pytest.mark.asyncio
async def test_get_honors_retry_after_on_429(monkeypatch):
    sleeps = _patch_sleep(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return _ok_response(request)

    slack = SlackClient("xoxp-test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        payload = await slack._get(client, "conversations.history", {"channel": "C1"})

    assert payload["ok"] is True
    assert sleeps == [7.0]


class FakeSlackClient:
    def __init__(self):
        self.history = {
            "C_BROKEN": httpx.ReadTimeout("slow slack"),
            "C_OK": [{"ts": "1893456000.000100", "user": "U_OTHER", "text": "please review the doc"}],
        }

    async def users_conversations(self, **kwargs):
        yield {"id": "C_BROKEN", "name": "broken"}
        yield {"id": "C_OK", "name": "ok"}

    async def conversations_history(self, channel, *, oldest=None):
        result = self.history[channel]
        if isinstance(result, Exception):
            raise result
        for msg in result:
            yield msg

    async def users_info(self, user_id):
        return {"name": "other"}

    async def get_permalink(self, channel, message_ts):
        return None


@pytest.mark.asyncio
async def test_fetch_skips_channel_that_keeps_failing():
    source = SlackSource(
        account_key="T1",
        access_token="xoxp-test",
        authed_user_id="U_ME",
        watermarks={"C_BROKEN": "1751000000.000000"},
    )
    source.client = FakeSlackClient()

    envelopes = [e async for e in source.fetch()]

    assert [e.external_id for e in envelopes] == ["C_OK:1893456000.000100"]
    assert source.next_watermarks["C_BROKEN"] == "1751000000.000000"
    assert source.next_watermarks["C_OK"] == "1893456000.000100"


def test_raise_for_splits_dead_token_from_channel_errors():
    with pytest.raises(SlackAuthError):
        _raise_for({"ok": False, "error": "token_revoked"})
    with pytest.raises(SlackAPIError) as exc_info:
        _raise_for({"ok": False, "error": "not_in_channel"})
    assert not isinstance(exc_info.value, SlackAuthError)


class RevokedSlackClient(FakeSlackClient):
    async def users_conversations(self, **kwargs):
        raise SlackAuthError("token_revoked")
        yield


@pytest.mark.asyncio
async def test_fetch_records_dead_token_instead_of_raising():
    source = SlackSource(account_key="T1", access_token="xoxp-test", authed_user_id="U_ME")
    source.client = RevokedSlackClient()

    envelopes = [e async for e in source.fetch()]

    assert envelopes == []
    assert source.auth_error == "token_revoked"


@pytest.mark.asyncio
async def test_fetch_stops_instead_of_skipping_channel_on_dead_token():
    client = FakeSlackClient()
    client.history["C_BROKEN"] = SlackAuthError("account_inactive")
    source = SlackSource(account_key="T1", access_token="xoxp-test", authed_user_id="U_ME")
    source.client = client

    envelopes = [e async for e in source.fetch()]

    assert envelopes == []
    assert source.auth_error == "account_inactive"
    assert "C_OK" not in source.next_watermarks
