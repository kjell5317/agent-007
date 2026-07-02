from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.task import open as open_svc  # noqa: E402


@pytest.mark.asyncio
async def test_open_linked_duplicate_runs_followup_instead_of_enqueue(monkeypatch):
    task_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    raw = SimpleNamespace(
        id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
        task_id=task_id,
        status="duplicate",
        agent_trace={"outcome": "no_change"},
        received_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    task = SimpleNamespace(id=task_id)
    calls = []
    published_inputs = []
    published_tasks = []

    async def fake_run_thread_followup(session, raw_arg, task_arg):
        calls.append((session, raw_arg, task_arg))
        return {"outcome": "closed", "task_id": str(task_arg.id)}

    async def fail_enqueue(*_args, **_kwargs):
        raise AssertionError("manual open should not enqueue fresh task creation")

    monkeypatch.setattr(open_svc.raw_inputs_store, "get", lambda _session, raw_id: raw)
    monkeypatch.setattr(
        open_svc.tasks_store,
        "get",
        lambda _session, tid: task if tid == task_id else None,
    )
    monkeypatch.setattr(open_svc, "run_thread_followup", fake_run_thread_followup)
    monkeypatch.setattr(open_svc, "enqueue", fail_enqueue)
    monkeypatch.setattr(
        open_svc,
        "publish_input",
        lambda _session, raw_id: published_inputs.append(raw_id),
    )
    monkeypatch.setattr(
        open_svc,
        "publish_task",
        lambda _session, tid: published_tasks.append(tid),
    )

    session = SimpleNamespace()
    await open_svc.open_task_from_input(session, raw.id, {})

    assert calls == [(session, raw, task)]
    assert published_inputs == [raw.id]
    assert published_tasks == [task_id]


@pytest.mark.asyncio
async def test_open_context_linked_input_runs_followup(monkeypatch):
    raw_id = uuid.UUID("20000000-0000-0000-0000-000000000002")
    context_id = uuid.UUID("20000000-0000-0000-0000-000000000003")
    task_id = uuid.UUID("10000000-0000-0000-0000-000000000002")
    raw = SimpleNamespace(
        id=raw_id,
        task_id=None,
        status="not_task",
        agent_trace={"outcome": "not_task"},
        received_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    context = SimpleNamespace(
        id=context_id,
        task_id=task_id,
        status="open",
        agent_trace={"outcome": "task_created"},
        received_at=datetime(2026, 7, 1, 12, 5, tzinfo=timezone.utc),
    )
    task = SimpleNamespace(id=task_id)
    calls = []

    def fake_raw_get(_session, lookup_id):
        return {raw_id: raw, context_id: context}.get(lookup_id)

    async def fake_run_thread_followup(session, raw_arg, task_arg):
        calls.append((session, raw_arg, task_arg))
        return {"outcome": "reopened", "task_id": str(task_arg.id)}

    async def fail_enqueue(*_args, **_kwargs):
        raise AssertionError("context-linked manual open should not create a new task")

    monkeypatch.setattr(open_svc.raw_inputs_store, "get", fake_raw_get)
    monkeypatch.setattr(
        open_svc.tasks_store,
        "get",
        lambda _session, tid: task if tid == task_id else None,
    )
    monkeypatch.setattr(open_svc, "run_thread_followup", fake_run_thread_followup)
    monkeypatch.setattr(open_svc, "enqueue", fail_enqueue)
    monkeypatch.setattr(open_svc, "publish_input", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(open_svc, "publish_task", lambda *_args, **_kwargs: None)

    session = SimpleNamespace()
    await open_svc.open_task_from_input(session, raw_id, {}, [context_id])

    assert calls == [(session, raw, task)]


@pytest.mark.asyncio
async def test_open_unlinked_input_still_enqueues_fresh_task(monkeypatch):
    raw_id = uuid.UUID("20000000-0000-0000-0000-000000000004")
    raw = SimpleNamespace(
        id=raw_id,
        task_id=None,
        status="not_task",
        agent_trace={"outcome": "not_task"},
        received_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    enqueued = []

    async def fake_enqueue(raw_input_id, user_fields, context_input_ids):
        enqueued.append((raw_input_id, user_fields, context_input_ids))

    async def fail_run_thread_followup(*_args, **_kwargs):
        raise AssertionError("unlinked raw input should create a fresh task")

    monkeypatch.setattr(
        open_svc.raw_inputs_store,
        "get",
        lambda _session, lookup_id: raw if lookup_id == raw_id else None,
    )
    monkeypatch.setattr(open_svc, "run_thread_followup", fail_run_thread_followup)
    monkeypatch.setattr(open_svc, "enqueue", fake_enqueue)

    await open_svc.open_task_from_input(
        SimpleNamespace(),
        raw_id,
        {"title": "Explicit title"},
    )

    assert enqueued == [(raw_id, {"title": "Explicit title"}, [])]
