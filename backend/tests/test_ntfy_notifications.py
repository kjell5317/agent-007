import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from app.api import notifications
from app.db import get_session
from app.services import ntfy, notify


def _settings():
    return SimpleNamespace(
        notification_provider="ntfy",
        ntfy_base_url="https://ntfy.sh",
        ntfy_topic="long-private-topic",
        ntfy_token="",
        notification_action_url="",
        notify_action_secret="callback-secret",
        home_assistant_action_secret="",
        task_default_url="https://007.example.org",
    )


def test_ntfy_action_buttons_target_existing_callback_with_scoped_signatures():
    settings = _settings()
    tag = f"task-{uuid.uuid4()}"
    actions = [
        {"action": notify.ACTION_CLOSE_TASK, "title": "Done"},
        {"action": notify.ACTION_DISMISS_TASK, "title": "Dismiss"},
        {"action": notify.ACTION_RESCHEDULE_TASK, "title": "Reschedule"},
    ]
    payload = ntfy.message_payload(
        settings, "Task", "Scheduled", url="https://007.example.org/#task/123",
        tag=tag, actions=actions, importance="high",
    )

    assert payload["topic"] == settings.ntfy_topic
    assert payload["priority"] == 4
    assert payload["click"] == "https://007.example.org/#task/123"
    assert payload["sequence_id"] == ntfy.sequence_id(tag)
    assert [item["label"] for item in payload["actions"]] == ["Done", "Dismiss", "Reschedule"]
    for item, source in zip(payload["actions"], actions):
        assert item["action"] == "http"
        assert item["url"] == "https://007.example.org/notifications/actions"
        assert item["method"] == "POST"
        assert item["clear"] is True
        body = json.loads(item["body"])
        assert body["action"] == source["action"]
        assert body["tag"] == tag
        assert ntfy.verify_action(body["action"], tag, body["expires"], body["signature"], settings.notify_action_secret)
        assert not ntfy.verify_action("CLOSE_TASK" if body["action"] != "CLOSE_TASK" else "DISMISS_TASK", tag, body["expires"], body["signature"], settings.notify_action_secret)

    kotx = ntfy.message_payload(
        settings, "PR", "Merge Pull Request", url=None, tag=tag,
        actions=[{"action": notify.ACTION_KOTX_MERGE, "title": "Merge"}], importance=None,
    )
    assert json.loads(kotx["actions"][0]["body"])["action"] == notify.ACTION_KOTX_MERGE


def test_ntfy_callback_rejects_tampering_and_expired_signatures(monkeypatch):
    settings = _settings()
    monkeypatch.setattr(notifications, "get_settings", lambda: settings)
    request = SimpleNamespace(headers={}, query_params={})
    tag = f"task-{uuid.uuid4()}"
    signed = ntfy.signed_action("CLOSE_TASK", tag, settings.notify_action_secret, now=1_800_000_000)
    payload = notifications.ActionPayload(**signed)

    # The explicit time above keeps this test independent of the current date.
    monkeypatch.setattr(ntfy.time, "time", lambda: 1_800_000_001)
    notifications._check_secret(request, payload)
    with pytest.raises(HTTPException) as wrong_action:
        notifications._check_secret(request, payload.model_copy(update={"action": "DISMISS_TASK"}))
    assert wrong_action.value.status_code == 401
    with pytest.raises(HTTPException):
        notifications._check_secret(request, payload.model_copy(update={"task_id": str(uuid.uuid4())}))
    monkeypatch.setattr(ntfy.time, "time", lambda: signed["expires"] + 1)
    with pytest.raises(HTTPException):
        notifications._check_secret(request, payload)


@pytest.mark.asyncio
async def test_ntfy_signed_done_button_uses_existing_action_handler(monkeypatch):
    settings = _settings()
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id)
    closed = []
    monkeypatch.setattr(notifications, "get_settings", lambda: settings)
    monkeypatch.setattr(notifications.tasks_store, "get", lambda *_args: task)

    async def fake_close(_session, received_id):
        closed.append(received_id)

    monkeypatch.setattr(notifications, "close_task_svc", fake_close)
    body = ntfy.signed_action(notify.ACTION_CLOSE_TASK, f"task-{task_id}", settings.notify_action_secret)
    request = SimpleNamespace(headers={"origin": "https://ntfy.sh"}, query_params={})
    response = Response()

    result = await notifications.handle_action(
        notifications.ActionPayload(**body), request=request, response=response, session=object(),
    )

    assert result["ok"] is True
    assert closed == [task_id]
    assert response.headers["access-control-allow-origin"] == "https://ntfy.sh"


def test_ntfy_action_preflight_allows_only_selected_ntfy_origin(monkeypatch):
    monkeypatch.setattr(notifications, "get_settings", _settings)
    good = SimpleNamespace(headers={"origin": "https://ntfy.sh"})
    assert notifications.action_preflight(good).headers["access-control-allow-methods"] == "POST, OPTIONS"
    with pytest.raises(HTTPException) as blocked:
        notifications.action_preflight(SimpleNamespace(headers={"origin": "https://other.example"}))
    assert blocked.value.status_code == 403


def test_ntfy_http_action_preflight_and_callback(monkeypatch):
    settings = _settings()
    task_id = uuid.uuid4()
    called = []
    monkeypatch.setattr(notifications, "get_settings", lambda: settings)
    monkeypatch.setattr(notifications.tasks_store, "get", lambda *_args: SimpleNamespace(id=task_id))

    async def fake_dismiss(_session, received_id):
        called.append(received_id)

    monkeypatch.setattr(notifications, "dismiss_task", fake_dismiss)
    app = FastAPI()
    app.include_router(notifications.router)
    app.dependency_overrides[get_session] = lambda: object()
    body = ntfy.signed_action(notify.ACTION_DISMISS_TASK, f"task-{task_id}", settings.notify_action_secret)

    with TestClient(app) as client:
        preflight = client.options(
            "/notifications/actions",
            headers={"Origin": "https://ntfy.sh", "Access-Control-Request-Method": "POST"},
        )
        response = client.post(
            "/notifications/actions", json=body,
            headers={"Origin": "https://ntfy.sh"},
        )

    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "https://ntfy.sh"
    assert response.status_code == 202
    assert response.headers["access-control-allow-origin"] == "https://ntfy.sh"
    assert called == [task_id]


@pytest.mark.asyncio
async def test_ntfy_transport_publishes_and_clears_same_sequence(monkeypatch):
    settings = _settings()
    requests = []
    real_client = httpx.AsyncClient

    def capture(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "abc"}, request=request)

    transport = httpx.MockTransport(capture)
    monkeypatch.setattr(ntfy.httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    tag = f"task-{uuid.uuid4()}"

    await ntfy.send(settings, "Task", "Scheduled", url=None, tag=tag, actions=None, importance=None)
    await ntfy.send(settings, "", "clear_notification", url=None, tag=tag, actions=None, importance=None)

    assert requests[0].method == "POST"
    assert requests[0].url == "https://ntfy.sh/"
    assert json.loads(requests[0].content)["sequence_id"] == ntfy.sequence_id(tag)
    assert requests[1].method == "PUT"
    assert str(requests[1].url) == f"https://ntfy.sh/{settings.ntfy_topic}/{ntfy.sequence_id(tag)}/clear"


@pytest.mark.asyncio
async def test_notify_routes_only_to_selected_provider(monkeypatch):
    settings = _settings()
    sent = []

    async def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(notify, "get_settings", lambda: settings)
    monkeypatch.setattr(ntfy, "send", fake_send)
    await notify.notify("Task", "Scheduled", tag="task-1")
    assert len(sent) == 1
    assert sent[0][0][:3] == (settings, "Task", "Scheduled")

    settings.notification_provider = "none"
    await notify.notify("Task", "Scheduled", tag="task-1")
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_home_assistant_transport_remains_available(monkeypatch):
    settings = SimpleNamespace(
        notification_provider="home_assistant",
        home_assistant_url="https://ha.example.org",
        home_assistant_token="ha-token",
        home_assistant_notify_service="mobile_app_phone",
    )
    requests = []
    real_client = httpx.AsyncClient

    def capture(request):
        requests.append(request)
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(notify, "get_settings", lambda: settings)
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(capture), **kwargs))
    await notify.notify("Task", "Scheduled", tag="task-1", actions=[{"action": "CLOSE_TASK", "title": "Done"}])

    assert str(requests[0].url) == "https://ha.example.org/api/services/notify/mobile_app_phone"
    assert json.loads(requests[0].content)["data"]["actions"][0]["action"] == "CLOSE_TASK"
