from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.auth.slack import _bundle_from_token_response
from app.db.clients.oauth_tokens import DecryptedToken
from app.services.input.slack import poll as slack_poll
from app.services.input.slack.client import SlackAuthError
from app.services.input.slack.source import SlackSource


class RevokedClient:
    async def users_conversations(self, **kwargs):
        raise SlackAuthError("token_revoked")
        yield


def _token(extra: dict) -> DecryptedToken:
    return DecryptedToken(
        id=1,
        provider="slack",
        account_key="T1:U1",
        access_token="xoxp-test",
        refresh_token=None,
        expires_at=None,
        scopes=[],
        extra={"team": {"name": "CSEE"}, "authed_user_id": "U1", **extra},
    )


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(extra={}, patches=[], notifications=[], sources=[])

    monkeypatch.setattr(
        slack_poll.oauth_tokens, "list_account_keys", lambda session, provider: ["T1:U1"]
    )
    monkeypatch.setattr(
        slack_poll.oauth_tokens,
        "get_decrypted",
        lambda session, provider, account_key: _token(state.extra),
    )

    def set_extra(session, *, provider, account_key, patch):
        state.patches.append(patch)
        state.extra = {**state.extra, **patch}

    monkeypatch.setattr(slack_poll.oauth_tokens, "set_extra", set_extra)

    async def send(message):
        state.notifications.append(message)

    monkeypatch.setattr(slack_poll.push, "send", send)

    def make_source(**kwargs):
        source = SlackSource(**kwargs)
        source.client = RevokedClient()
        state.sources.append(source)
        return source

    monkeypatch.setattr(slack_poll, "SlackSource", make_source)
    return state


@pytest.mark.asyncio
async def test_dead_token_pauses_account_and_notifies_once(harness):
    session = SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    first = await slack_poll.poll(session, None)
    second = await slack_poll.poll(session, None)

    assert {"auth_error": "token_revoked"} in harness.patches
    assert first["errors"] == [{"auth": "token_revoked"}]
    assert len(harness.notifications) == 1
    assert "CSEE" in harness.notifications[0].title
    assert second["errors"] == []
    assert len(harness.sources) == 1


def test_reauth_bundle_lifts_the_pause():
    bundle = _bundle_from_token_response(
        {"authed_user": {"id": "U1", "access_token": "xoxp-new", "scope": ""}, "team": {"id": "T1"}}
    )

    merged = {"auth_error": "token_revoked", **bundle.extra}

    assert merged["auth_error"] is None
