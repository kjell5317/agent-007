from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.api import tasks as tasks_api  # noqa: E402


def _task(task_id: uuid.UUID):
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=task_id,
        title="Read linked inputs",
        description=None,
        link=None,
        due_date=now,
        scheduled_date=now,
        estimation=30,
        location=None,
        label=None,
        created_at=now,
        updated_at=now,
    )


def _raw(
    task,
    *,
    source: str,
    status: str,
    received_at: datetime,
    external_id: str | None = None,
    source_metadata: dict | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        source=source,
        external_id=external_id,
        content=f"{source} content",
        source_metadata=source_metadata or {},
        received_at=received_at,
        processed_at=received_at,
        status=status,
        task_id=task.id,
        task=task,
        agent_trace=None,
    )


@pytest.mark.asyncio
async def test_list_tasks_includes_all_linked_inputs_newest_first(monkeypatch):
    task_id = uuid.uuid4()
    task = _task(task_id)
    older = _raw(
        task,
        source="gmail",
        status="open",
        received_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        source_metadata={"thread_id": "thread-a", "account": "me@example.com"},
    )
    newer_duplicate = _raw(
        task,
        source="slack",
        status="duplicate",
        received_at=datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc),
        external_id="C123:1719831600.000100",
        source_metadata={"channel_id": "C123"},
    )

    monkeypatch.setattr(
        tasks_api.tasks_store,
        "list_",
        lambda *_args, **_kw: [(task, "open")],
    )
    monkeypatch.setattr(
        tasks_api.tasks_store,
        "is_manual_for",
        lambda *_args: {task_id: False},
    )
    monkeypatch.setattr(tasks_api.raw_inputs_store, "latest_for_task", lambda *_args: older)
    monkeypatch.setattr(
        tasks_api.raw_inputs_store,
        "list_for_task",
        lambda *_args: [newer_duplicate, older],
    )

    reads = await tasks_api.list_tasks(session=object())

    assert len(reads) == 1
    payload = reads[0].model_dump(mode="json")
    assert payload["source_url"].startswith("https://mail.google.com/")
    assert [r["id"] for r in payload["raw_inputs"]] == [
        str(newer_duplicate.id),
        str(older.id),
    ]
    assert payload["raw_inputs"][0]["status"] == "duplicate"
    assert payload["raw_inputs"][0]["source_url"].startswith(
        "https://slack.com/app_redirect?"
    )
    assert payload["raw_inputs"][1]["task_title"] == task.title


@pytest.mark.asyncio
async def test_get_task_includes_linked_inputs(monkeypatch):
    task_id = uuid.uuid4()
    task = _task(task_id)
    raw = _raw(
        task,
        source="gmail",
        status="open",
        received_at=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        source_metadata={"thread_id": "thread-b"},
    )

    monkeypatch.setattr(tasks_api.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(
        tasks_api.tasks_store,
        "latest_status_for",
        lambda *_args: {task_id: "open"},
    )
    monkeypatch.setattr(
        tasks_api.tasks_store,
        "is_manual_for",
        lambda *_args: {task_id: False},
    )
    monkeypatch.setattr(tasks_api.raw_inputs_store, "latest_for_task", lambda *_args: raw)
    monkeypatch.setattr(tasks_api.raw_inputs_store, "list_for_task", lambda *_args: [raw])

    read = await tasks_api.get_task(task_id, session=object())

    payload = read.model_dump(mode="json")
    assert payload["id"] == str(task_id)
    assert len(payload["raw_inputs"]) == 1
    assert payload["raw_inputs"][0]["id"] == str(raw.id)
    assert payload["raw_inputs"][0]["source_url"] == (
        "https://mail.google.com/mail/u/0/#all/thread-b"
    )
