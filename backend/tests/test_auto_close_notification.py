from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import dispatch  # noqa: E402
from app.api import notifications  # noqa: E402
from app.services import notify as notify_service  # noqa: E402


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_notify(**kwargs):
        calls.append(kwargs)

    async def fake_close(_session, _task_id):
        return None

    monkeypatch.setattr(
        notify_service, "get_settings", lambda: SimpleNamespace(public_url="https://example.test/app")
    )
    monkeypatch.setattr(notify_service, "notify", fake_notify)
    monkeypatch.setattr(dispatch, "close_task_svc", fake_close)
    return calls


@pytest.mark.asyncio
async def test_agent_close_notifies_with_reopen_button(sent):
    task = SimpleNamespace(id=uuid.uuid4(), title="Pay invoice")

    result = await dispatch.apply_task_action(
        SimpleNamespace(), task, "update_task",
        {"status": "closed", "reason": "Reply confirms the invoice was paid."},
    )

    assert result["outcome"] == "closed"
    assert sent == [{
        "title": "Auto-closed: Pay invoice",
        "message": "Reply confirms the invoice was paid.",
        "url": f"https://example.test/app#task/{task.id}",
        "tag": f"task-{task.id}",
        "actions": [{"action": notify_service.ACTION_REOPEN_TASK, "title": "Reopen"}],
    }]


@pytest.mark.asyncio
async def test_user_requested_close_sends_no_notification(sent):
    await dispatch.apply_task_action(
        SimpleNamespace(), SimpleNamespace(id=uuid.uuid4(), title="Pay invoice"),
        "update_task", {"status": "closed"}, user_requested=True,
    )

    assert sent == []


@pytest.mark.asyncio
async def test_reopen_button_reopens_only_a_still_closed_task(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), title="Pay invoice")
    status = {"value": "closed"}
    reopened: list[uuid.UUID] = []

    async def fake_reopen(_session, task_id):
        reopened.append(task_id)
        status["value"] = "open"

    monkeypatch.setattr(notifications, "get_settings", lambda: SimpleNamespace(home_assistant_action_secret=""))
    monkeypatch.setattr(notifications.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(
        notifications.tasks_store, "latest_status_for", lambda *_args: {task.id: status["value"]}
    )
    monkeypatch.setattr(notifications, "reopen_task", fake_reopen)
    payload = notifications.ActionPayload(
        action=notifications.ACTION_REOPEN_TASK, tag=f"task-{task.id}"
    )
    request = SimpleNamespace(headers={}, query_params={})

    first = await notifications.handle_action(payload, request, session=SimpleNamespace())
    stale = await notifications.handle_action(payload, request, session=SimpleNamespace())

    assert first["ok"] is True and stale["ok"] is True
    assert reopened == [task.id]
