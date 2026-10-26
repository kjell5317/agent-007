from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.api import oauth as oauth_api  # noqa: E402
from app.config.settings import Settings  # noqa: E402
from app.services import ntfy  # noqa: E402


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, database_url="postgresql://x", **kw)


def test_public_url_is_the_caddy_domain_unless_a_base_url_is_set(monkeypatch):
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("APP_DOMAIN", raising=False)
    assert _settings(app_domain="007.example.org").public_url == "https://007.example.org"
    assert _settings(app_domain="007.example.org", base_url="http://localhost:5173/").public_url == (
        "http://localhost:5173"
    )


def test_callbacks_hang_off_the_public_url(monkeypatch):
    settings = SimpleNamespace(public_url="https://007.example.org")
    monkeypatch.setattr(oauth_api, "get_settings", lambda: settings)
    assert oauth_api._redirect_uri("google_health") == (
        "https://007.example.org/oauth/google_health/callback"
    )
    assert ntfy.action_url(settings) == "https://007.example.org/notifications/actions"
