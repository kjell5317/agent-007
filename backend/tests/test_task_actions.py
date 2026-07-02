from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.api import tasks as tasks_api  # noqa: E402
from app.services.kotx import runs as kotx_runs  # noqa: E402


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


def _task(*, label: str | None = None, link: str | None = None):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        title="Implement task action",
        description="Use deterministic backend behavior.",
        link=link,
        due_date=now,
        scheduled_date=now,
        estimation=30,
        location=None,
        label=label,
        created_at=now,
        updated_at=now,
    )


def _patch_read_helpers(monkeypatch, task):
    monkeypatch.setattr(tasks_api.raw_inputs_store, "latest_for_task", lambda *_args: None)
    monkeypatch.setattr(tasks_api.raw_inputs_store, "list_for_task", lambda *_args: [])
    monkeypatch.setattr(
        tasks_api.tasks_store,
        "latest_status_for",
        lambda _session, ids: {task_id: "open" for task_id in ids},
    )
    monkeypatch.setattr(
        tasks_api.tasks_store,
        "is_manual_for",
        lambda _session, ids: {task_id: False for task_id in ids},
    )


@pytest.mark.asyncio
async def test_reschedule_task_calls_scheduler_and_publishes(monkeypatch):
    task = _task()
    session = FakeSession()
    calls = []
    published = []

    async def fake_schedule_task(_session, row):
        calls.append((_session, row))
        row.scheduled_date = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
        return (row.scheduled_date, datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc))

    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(tasks_api, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(tasks_api, "publish_task", lambda _session, task_id: published.append(task_id))
    _patch_read_helpers(monkeypatch, task)

    read = await tasks_api.reschedule_task(task.id, session=session)

    assert calls == [(session, task)]
    assert published == [task.id]
    assert read.scheduled_date == datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_reschedule_task_returns_clear_error_when_unschedulable(monkeypatch):
    task = _task()

    async def fake_schedule_task(_session, _task):
        return None

    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(tasks_api, "schedule_task", fake_schedule_task)

    with pytest.raises(HTTPException) as exc:
        await tasks_api.reschedule_task(task.id, session=FakeSession())

    assert exc.value.status_code == 400
    assert exc.value.detail == "Task could not be scheduled"


@pytest.mark.asyncio
async def test_create_issue_run_maps_csee_to_repo_and_payload(monkeypatch):
    task = _task(label="CSEE")
    calls = []

    class FakeResponse:
        status_code = 201

        def json(self):
            return {"issueUrl": "https://github.com/askLio/CSEE-strategic-negotiation-agent/issues/7"}

    class FakeAsyncClient:
        def __init__(self, *, timeout, headers):
            self.timeout = timeout
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json):
            calls.append({"url": url, "json": json, "headers": self.headers})
            return FakeResponse()

    monkeypatch.setattr(
        kotx_runs,
        "get_settings",
        lambda: SimpleNamespace(kotx_base_url="https://kotx.example", kotx_api_token="kotx_test"),
    )
    monkeypatch.setattr(kotx_runs.httpx, "AsyncClient", FakeAsyncClient)

    run = await kotx_runs.create_issue_run(task)

    assert run.issue_url == "https://github.com/askLio/CSEE-strategic-negotiation-agent/issues/7"
    assert calls == [
        {
            "url": "https://kotx.example/api/runs",
            "json": {
                "repo": "CSEE",
                "title": "Implement task action",
                "body": "Use deterministic backend behavior.",
            },
            "headers": {
                "Authorization": "Bearer kotx_test",
            },
        }
    ]


@pytest.mark.asyncio
async def test_create_issue_run_maps_social_ai_to_kotx_alias(monkeypatch):
    task = _task(label="Social AI", link="https://example.com/context")
    calls = []

    class FakeResponse:
        status_code = 201

        def json(self):
            return {"issueUrl": "https://github.com/TUM-Social-AI/AflaConnect/issues/5"}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json):
            calls.append((url, json))
            return FakeResponse()

    monkeypatch.setattr(
        kotx_runs,
        "get_settings",
        lambda: SimpleNamespace(kotx_base_url="https://kotx.example/", kotx_api_token="kotx_test"),
    )
    monkeypatch.setattr(kotx_runs.httpx, "AsyncClient", FakeAsyncClient)

    await kotx_runs.create_issue_run(task)

    assert calls[0][0] == "https://kotx.example/api/runs"
    assert calls[0][1]["repo"] == "Social Ai"


@pytest.mark.asyncio
async def test_create_github_issue_propagates_kotx_error_status(monkeypatch):
    task = _task(label="CSEE")

    class FakeResponse:
        status_code = 422
        reason_phrase = "Unprocessable Entity"
        text = '{"error":"repo not allowed"}'

        def json(self):
            return {"error": "repo not allowed"}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, *, json):
            return FakeResponse()

    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(
        kotx_runs,
        "get_settings",
        lambda: SimpleNamespace(kotx_base_url="https://kotx.example", kotx_api_token="kotx_test"),
    )
    monkeypatch.setattr(kotx_runs.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await tasks_api.create_github_issue(task.id, session=FakeSession())

    assert exc.value.status_code == 422
    assert exc.value.detail == "kotx run creation failed: repo not allowed"


@pytest.mark.asyncio
async def test_create_github_issue_denies_existing_github_link(monkeypatch):
    task = _task(label="CSEE", link="https://github.com/askLio/CSEE-strategic-negotiation-agent/issues/1")
    called = False

    async def fake_create_issue_run(_task):
        nonlocal called
        called = True

    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(tasks_api, "create_issue_run", fake_create_issue_run)

    with pytest.raises(HTTPException) as exc:
        await tasks_api.create_github_issue(task.id, session=FakeSession())

    assert exc.value.status_code == 409
    assert exc.value.detail == "Task already has a GitHub URL"
    assert called is False


@pytest.mark.asyncio
async def test_create_github_issue_rejects_unsupported_label(monkeypatch):
    task = _task(label="Other")
    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)

    with pytest.raises(HTTPException) as exc:
        await tasks_api.create_github_issue(task.id, session=FakeSession())

    assert exc.value.status_code == 400
    assert "only supported for CSEE and Social AI" in exc.value.detail


@pytest.mark.asyncio
async def test_create_github_issue_rejects_missing_label(monkeypatch):
    task = _task(label=None)
    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)

    with pytest.raises(HTTPException) as exc:
        await tasks_api.create_github_issue(task.id, session=FakeSession())

    assert exc.value.status_code == 400
    assert "only supported for CSEE and Social AI" in exc.value.detail


@pytest.mark.asyncio
async def test_create_github_issue_stores_url_commits_and_publishes(monkeypatch):
    task = _task(label="Social AI", link="https://example.com/context")
    session = FakeSession()
    published = []

    async def fake_create_issue_run(_task):
        return SimpleNamespace(issue_url="https://github.com/TUM-Social-AI/AflaConnect/issues/5")

    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(tasks_api, "create_issue_run", fake_create_issue_run)
    monkeypatch.setattr(tasks_api, "publish_task", lambda _session, task_id: published.append(task_id))
    _patch_read_helpers(monkeypatch, task)

    read = await tasks_api.create_github_issue(task.id, session=session)

    assert task.link == "https://github.com/TUM-Social-AI/AflaConnect/issues/5"
    assert session.commits == 1
    assert published == [task.id]
    assert read.link == task.link
