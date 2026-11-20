from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.api import tasks as tasks_api  # noqa: E402
from app.db.clients import raw_inputs as raw_inputs_store  # noqa: E402
from app.services.kotx import runs as kotx_runs  # noqa: E402
from app.services.task import reopen as reopen_svc  # noqa: E402
from app.services.task import dismiss as dismiss_svc  # noqa: E402


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid.UUID("10000000-0000-0000-0000-000000000002")
        if getattr(row, "received_at", None) is None:
            row.received_at = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def refresh(self, _row):
        return None


def _task(*, label: str | None = None, link: str | None = None):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        title="Implement task action",
        description="Use deterministic backend behavior.",
        link=link,
        due_date=now,
        scheduled_date=now,
        calendar_event_id="evt_existing",
        schedule_state="placed",
        estimation=30,
        location=None,
        label=label,
        created_at=now,
        updated_at=now,
    )


def _patch_read_helpers(monkeypatch, task):
    monkeypatch.setattr(raw_inputs_store, "latest_for_task", lambda *_args: None)
    monkeypatch.setattr(raw_inputs_store, "list_for_task", lambda *_args: [])
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
async def test_reopen_task_endpoint_accepts_queued_followup(monkeypatch):
    raw_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
    raw = SimpleNamespace(id=raw_id)
    task_id = uuid.UUID("30000000-0000-0000-0000-000000000001")
    calls = []

    async def fake_enqueue_reopen_task(session, queued_task_id):
        calls.append((session, queued_task_id))
        return raw

    session = FakeSession()
    monkeypatch.setattr(tasks_api, "enqueue_reopen_task", fake_enqueue_reopen_task)

    accepted = await tasks_api.reopen_task(task_id, session=session)

    assert accepted.raw_input_id == raw_id
    assert accepted.status == "processing"
    assert calls == [(session, task_id)]


@pytest.mark.asyncio
async def test_reopen_task_service_creates_fresh_followup_input(monkeypatch):
    task = _task()
    anchor = SimpleNamespace(
        id=uuid.UUID("40000000-0000-0000-0000-000000000001"),
        status="closed",
        processed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    session = FakeSession()
    enqueued = []
    published = []

    async def fake_enqueue_followup(raw_input_id, followup_task_id):
        enqueued.append((raw_input_id, followup_task_id))

    monkeypatch.setattr(reopen_svc.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(
        reopen_svc.raw_inputs_store,
        "latest_for_task",
        lambda *_args: pytest.fail("queued reopen must not mutate the anchor input"),
    )
    monkeypatch.setattr(reopen_svc, "_enqueue_followup", fake_enqueue_followup)
    monkeypatch.setattr(reopen_svc, "publish_input", lambda _session, raw_id: published.append(raw_id))

    raw = await reopen_svc.enqueue_reopen_task(session, task.id)

    assert raw in session.added
    assert raw.source == "manual"
    assert raw.status == "processing"
    assert raw.task_id is None
    assert "Re-open this task" in raw.content
    assert "future due date" in raw.content
    assert raw.source_metadata == {
        "manual": True,
        "action": "reopen_task",
        "task_id": str(task.id),
    }
    assert enqueued == [(raw.id, task.id)]
    assert published == [raw.id]
    assert anchor.status == "closed"


@pytest.mark.asyncio
async def test_dismiss_keeps_task_and_input_links_for_reopen(monkeypatch):
    task = _task()
    anchor = SimpleNamespace(
        id=uuid.uuid4(), task_id=task.id, status="open",
        processed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    followup = SimpleNamespace(id=uuid.uuid4(), task_id=task.id, status="duplicate")
    session = FakeSession()
    published = []

    async def noop(*_args):
        pass

    monkeypatch.setattr(dismiss_svc.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(dismiss_svc.raw_inputs_store, "latest_for_task", lambda *_args: anchor)
    monkeypatch.setattr(dismiss_svc, "vacated_commute_window", lambda *_args: None)
    monkeypatch.setattr(dismiss_svc, "delete_task_event", noop)
    monkeypatch.setattr(dismiss_svc, "clear_task_notifications", noop)
    monkeypatch.setattr(dismiss_svc, "replan_vacated_window", noop)
    monkeypatch.setattr(dismiss_svc, "publish_task", lambda _session, id: published.append(("task", id)))
    monkeypatch.setattr(dismiss_svc, "publish_input", lambda _session, id: published.append(("input", id)))
    monkeypatch.setattr(dismiss_svc, "publish_task_removed", lambda _id: pytest.fail("task was removed"))

    monkeypatch.setattr("app.services.task.work.stop_running", lambda *_a, **_kw: None)
    await dismiss_svc.dismiss_task(session, task.id)

    assert anchor.status == "not_task"
    assert anchor.processed_at == datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    assert anchor.task_id == followup.task_id == task.id
    assert session.commits == 1
    assert published == [("task", task.id), ("input", anchor.id)]


@pytest.mark.asyncio
async def test_create_issue_run_sends_the_label_repo_and_payload(monkeypatch):
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

    run = await kotx_runs.create_issue_run(task, repo="askLio/CSEE-strategic-negotiation-agent")

    assert run.issue_url == "https://github.com/askLio/CSEE-strategic-negotiation-agent/issues/7"
    assert calls == [
        {
            "url": "https://kotx.example/api/runs",
            "json": {
                "repo": "askLio/CSEE-strategic-negotiation-agent",
                "title": "Implement task action",
                "body": "Use deterministic backend behavior.",
            },
            "headers": {
                "Authorization": "Bearer kotx_test",
            },
        }
    ]
