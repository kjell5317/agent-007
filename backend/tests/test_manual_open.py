from __future__ import annotations

import os
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.task import open as open_svc  # noqa: E402
from app.services.task import queue as queue_svc  # noqa: E402


@pytest.mark.asyncio
async def test_open_linked_duplicate_enqueues_followup(monkeypatch):
    task_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    raw = SimpleNamespace(
        id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
        task_id=task_id,
        status="duplicate",
        agent_trace={"outcome": "no_change"},
        received_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    task = SimpleNamespace(id=task_id)
    enqueued = []

    async def fake_enqueue(raw_input_id, user_fields, context_input_ids, followup_task_id=None):
        enqueued.append((raw_input_id, user_fields, context_input_ids, followup_task_id))

    monkeypatch.setattr(open_svc.raw_inputs_store, "get", lambda _session, raw_id: raw)
    monkeypatch.setattr(
        open_svc.tasks_store,
        "get",
        lambda _session, tid: task if tid == task_id else None,
    )
    monkeypatch.setattr(open_svc, "enqueue", fake_enqueue)

    await open_svc.open_task_from_input(SimpleNamespace(), raw.id, {})

    assert enqueued == [(raw.id, {}, [], task_id)]


@pytest.mark.asyncio
async def test_open_context_linked_input_enqueues_followup(monkeypatch):
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
    enqueued = []

    async def fake_enqueue(raw_input_id, user_fields, context_input_ids, followup_task_id=None):
        enqueued.append((raw_input_id, user_fields, context_input_ids, followup_task_id))

    monkeypatch.setattr(
        open_svc.raw_inputs_store,
        "get",
        lambda _session, lookup_id: {raw_id: raw, context_id: context}.get(lookup_id),
    )
    monkeypatch.setattr(
        open_svc.tasks_store,
        "get",
        lambda _session, tid: task if tid == task_id else None,
    )
    monkeypatch.setattr(open_svc, "enqueue", fake_enqueue)

    await open_svc.open_task_from_input(SimpleNamespace(), raw_id, {}, [context_id])

    assert enqueued == [(raw_id, {}, [context_id], task_id)]


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

    async def fake_enqueue(raw_input_id, user_fields, context_input_ids, followup_task_id=None):
        enqueued.append((raw_input_id, user_fields, context_input_ids, followup_task_id))

    monkeypatch.setattr(
        open_svc.raw_inputs_store,
        "get",
        lambda _session, lookup_id: raw if lookup_id == raw_id else None,
    )
    monkeypatch.setattr(open_svc, "enqueue", fake_enqueue)

    await open_svc.open_task_from_input(
        SimpleNamespace(),
        raw_id,
        {"title": "Explicit title"},
    )

    assert enqueued == [(raw_id, {"title": "Explicit title"}, [], None)]


@pytest.mark.asyncio
async def test_worker_runs_followup_for_queued_item(monkeypatch):
    import app.agent.thread.runner as thread_runner

    task_id = uuid.UUID("10000000-0000-0000-0000-000000000003")
    raw = SimpleNamespace(
        id=uuid.UUID("20000000-0000-0000-0000-000000000005"),
        task_id=task_id,
        status="not_task",
        agent_trace={"outcome": "not_task"},
        processed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    task = SimpleNamespace(id=task_id)
    session = SimpleNamespace()
    followups = []
    published_inputs = []
    published_tasks = []

    async def fake_followup(_session, raw_arg, task_arg):
        followups.append((raw_arg, task_arg))
        return {"outcome": "reopened", "task_id": str(task_arg.id)}

    monkeypatch.setattr(queue_svc, "SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(queue_svc.raw_inputs_store, "get", lambda _s, _id: raw)
    monkeypatch.setattr(
        queue_svc.tasks_store,
        "get",
        lambda _s, tid: task if tid == task_id else None,
    )
    monkeypatch.setattr(thread_runner, "run_thread_followup", fake_followup)
    monkeypatch.setattr(
        queue_svc, "publish_input", lambda _s, rid: published_inputs.append(rid)
    )
    monkeypatch.setattr(
        queue_svc, "publish_task", lambda _s, tid: published_tasks.append(tid)
    )

    await queue_svc._process(raw.id, {}, [], task_id)

    assert followups == [(raw, task)]
    assert published_inputs == [raw.id]
    assert published_tasks == [task_id]
