from types import SimpleNamespace

import pytest

from app.api import oauth


@pytest.mark.asyncio
async def test_slack_apps_returns_configured_names_without_credentials(monkeypatch):
    monkeypatch.setattr(
        oauth,
        "get_settings",
        lambda: SimpleNamespace(
            slack_apps={
                "social": {"client_id": "id", "client_secret": "secret"},
                "csee": {"client_id": "id", "client_secret": "secret"},
                "cs150B": {"client_id": "id", "client_secret": "secret"},
            }
        ),
    )

    assert await oauth.slack_apps() == ["social", "csee", "cs150B"]
