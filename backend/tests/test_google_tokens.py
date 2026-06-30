from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.auth import google_tokens  # noqa: E402
from app.auth.base import TokenBundle  # noqa: E402
from app.api import auth as auth_api  # noqa: E402
from app.db.clients.oauth_tokens import DecryptedToken  # noqa: E402


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _token(
    *,
    access_token: str = "access",
    refresh_token: str | None = "refresh",
    expires_at: datetime | None = None,
    extra: dict | None = None,
) -> DecryptedToken:
    return DecryptedToken(
        id=object(),
        provider="google",
        account_key="user@example.com",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=["scope"],
        extra=extra or {},
    )


@pytest.mark.asyncio
async def test_get_fresh_google_token_refreshes_expired_token_and_preserves_extra(
    monkeypatch,
):
    session = FakeSession()
    old_token = _token(
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        extra={"history_id": "123", "calendar_updated_min": "2026-06-30T00:00:00+00:00"},
    )
    refreshed_token = _token(
        access_token="new-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        extra=old_token.extra,
    )
    decrypted = [old_token, refreshed_token]
    refresh_calls = []
    upserts = []

    def fake_get_decrypted(session_arg, *, provider, account_key):
        assert session_arg is session
        assert provider == "google"
        assert account_key == "user@example.com"
        return decrypted.pop(0)

    def fake_upsert(session_arg, *, provider, account_key, bundle, extra_merge):
        upserts.append(
            {
                "session": session_arg,
                "provider": provider,
                "account_key": account_key,
                "bundle": bundle,
                "extra_merge": extra_merge,
            }
        )

    class FakeProvider:
        async def refresh(self, refresh_token):
            refresh_calls.append(refresh_token)
            return TokenBundle(
                access_token="new-access",
                refresh_token=None,
                expires_in=3600,
                scopes=["scope"],
                extra={"token_type": "Bearer"},
            )

    monkeypatch.setattr(google_tokens.oauth_tokens, "get_decrypted", fake_get_decrypted)
    monkeypatch.setattr(google_tokens.oauth_tokens, "upsert", fake_upsert)
    monkeypatch.setattr(google_tokens, "get_provider", lambda name: FakeProvider)

    token = await google_tokens.get_fresh_google_token(
        session,
        account_key="user@example.com",
    )

    assert token.access_token == "new-access"
    assert refresh_calls == ["old-refresh"]
    assert session.commits == 1
    assert upserts == [
        {
            "session": session,
            "provider": "google",
            "account_key": "user@example.com",
            "bundle": TokenBundle(
                access_token="new-access",
                refresh_token=None,
                expires_in=3600,
                scopes=["scope"],
                extra={"token_type": "Bearer"},
            ),
            "extra_merge": old_token.extra,
        }
    ]


@pytest.mark.asyncio
async def test_get_fresh_google_token_does_not_refresh_valid_token(monkeypatch):
    session = FakeSession()
    valid_token = _token(expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    refresh_calls = []
    upsert_calls = []

    monkeypatch.setattr(
        google_tokens.oauth_tokens,
        "get_decrypted",
        lambda *args, **kwargs: valid_token,
    )
    monkeypatch.setattr(
        google_tokens.oauth_tokens,
        "upsert",
        lambda *args, **kwargs: upsert_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        google_tokens,
        "get_provider",
        lambda name: SimpleNamespace(refresh=lambda refresh_token: refresh_calls.append(refresh_token)),
    )

    token = await google_tokens.get_fresh_google_token(session, account_key="user@example.com")

    assert token is valid_token
    assert refresh_calls == []
    assert upsert_calls == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_get_fresh_google_token_requires_reauthorization_without_refresh_token(
    monkeypatch,
):
    session = FakeSession()
    expired_token = _token(
        refresh_token=None,
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    monkeypatch.setattr(
        google_tokens.oauth_tokens,
        "get_decrypted",
        lambda *args, **kwargs: expired_token,
    )

    with pytest.raises(google_tokens.GoogleReauthorizationRequired):
        await google_tokens.get_fresh_google_token(session, account_key="user@example.com")

    assert session.commits == 0


@pytest.mark.asyncio
async def test_whoami_refreshes_allowlisted_session(monkeypatch):
    calls = []

    async def fake_get_fresh_google_token(session, *, account_key):
        calls.append((session, account_key))
        return _token()

    monkeypatch.setattr(
        auth_api,
        "get_settings",
        lambda: SimpleNamespace(auth_allowed_emails=["user@example.com"]),
    )
    monkeypatch.setattr(auth_api, "get_fresh_google_token", fake_get_fresh_google_token)
    request = SimpleNamespace(session={"email": "USER@example.com"})
    session = object()

    response = await auth_api.whoami(request, session=session)

    assert response == {"email": "USER@example.com"}
    assert calls == [(session, "user@example.com")]
    assert request.session == {"email": "USER@example.com"}


@pytest.mark.asyncio
async def test_whoami_clears_session_when_google_token_needs_reauthorization(
    monkeypatch,
):
    async def fake_get_fresh_google_token(session, *, account_key):
        raise google_tokens.GoogleReauthorizationRequired("reauthorize")

    monkeypatch.setattr(
        auth_api,
        "get_settings",
        lambda: SimpleNamespace(auth_allowed_emails=["user@example.com"]),
    )
    monkeypatch.setattr(auth_api, "get_fresh_google_token", fake_get_fresh_google_token)
    request = SimpleNamespace(session={"email": "user@example.com"})

    response = await auth_api.whoami(request, session=object())

    assert response == {"email": None}
    assert request.session == {}
