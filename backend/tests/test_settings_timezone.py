from types import SimpleNamespace

import pytest

from app.api import settings as settings_api


@pytest.mark.asyncio
async def test_settings_expose_notification_timezone(monkeypatch):
    monkeypatch.setattr(
        settings_api, "get_app_settings",
        lambda: SimpleNamespace(user_timezone="America/Los_Angeles"),
    )

    result = await settings_api.get_settings()

    assert result.user_timezone == "America/Los_Angeles"
