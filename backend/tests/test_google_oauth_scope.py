from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.auth import google  # noqa: E402

_HEALTH_SCOPE = "https://www.googleapis.com/auth/googlehealth.sleep.readonly"


def _authorize_query(provider: google.GoogleOAuthProvider, monkeypatch) -> dict:
    monkeypatch.setattr(
        google,
        "get_settings",
        lambda: SimpleNamespace(google_oauth_client_id="client-id"),
    )
    url = provider.authorize_url(state="state-token", redirect_uri="http://localhost/callback")
    return parse_qs(urlparse(url).query)


def test_main_google_grant_excludes_health_and_unions_scopes(monkeypatch):
    query = _authorize_query(google.GoogleOAuthProvider(), monkeypatch)

    scopes = query["scope"][0].split()
    assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
    assert "https://www.googleapis.com/auth/calendar.events" in scopes
    # Health lives on its own grant — the Health API rejects mixed-scope tokens.
    assert _HEALTH_SCOPE not in scopes
    assert query["include_granted_scopes"] == ["true"]
    assert query["prompt"] == ["consent"]


def test_health_grant_is_health_only_without_scope_union(monkeypatch):
    query = _authorize_query(google.GoogleHealthOAuthProvider(), monkeypatch)

    scopes = query["scope"][0].split()
    assert scopes == ["openid", "email", _HEALTH_SCOPE]
    # Must NOT union past grants, or Gmail/Calendar would leak back into the token.
    assert "include_granted_scopes" not in query
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
