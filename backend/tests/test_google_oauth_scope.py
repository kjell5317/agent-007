from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.auth import google  # noqa: E402


def test_google_authorize_url_includes_sleep_scope(monkeypatch):
    monkeypatch.setattr(
        google,
        "get_settings",
        lambda: SimpleNamespace(google_oauth_client_id="client-id"),
    )

    url = google.GoogleOAuthProvider().authorize_url(
        state="state-token",
        redirect_uri="http://localhost/callback",
    )
    query = parse_qs(urlparse(url).query)

    scopes = query["scope"][0].split()
    assert "https://www.googleapis.com/auth/fitness.sleep.read" in scopes
    assert query["include_granted_scopes"] == ["true"]
    assert query["prompt"] == ["consent"]
