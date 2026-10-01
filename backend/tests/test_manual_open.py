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
from app.agent.helpers.llm import ToolCall  # noqa: E402


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

    await open_svc.open_task_from_input(SimpleNamespace(commit=lambda: None), raw.id, {})

    assert enqueued == [(raw.id, {}, [], task_id)]
    assert raw.agent_trace["manual_override"]["outcome"] == "processing"


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

    async def fake_followup(_session, raw_arg, task_arg, *, require_change=False):
        assert require_change is True
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


@pytest.mark.asyncio
async def test_thread_followup_trace_records_current_task_context(monkeypatch):
    import app.agent.thread.runner as thread_runner

    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    raw = SimpleNamespace(
        id=uuid.UUID("20000000-0000-0000-0000-000000000006"),
        source="manual",
        source_metadata={"action": "reopen_task"},
        content="Re-open this task and choose an appropriate new future due date.",
    )
    task = SimpleNamespace(
        id=uuid.UUID("10000000-0000-0000-0000-000000000006"),
        title="Submit renewal paperwork",
        description="Use the current account packet.",
        due_date=now,
        scheduled_date=None,
        estimation=45,
        location="Office",
        link="https://example.test/task",
        label="Admin",
    )
    finalized = {}

    async def fake_chat(messages, *_args, **_kwargs):
        assert "Current task:" in (messages[0].text or "")
        assert "status: closed" in (messages[0].text or "")
        assert "title: Submit renewal paperwork" in (messages[0].text or "")
        return SimpleNamespace(
            text="",
            stop_reason="tool_use",
            usage={},
            provider="test",
            model="test-model",
            tool_calls=(
                ToolCall(
                    id="toolu_1",
                    name="update_task",
                    input={
                        "status": "open",
                        "due_date": "2026-07-02T12:00:00+00:00",
                    },
                ),
            ),
        )

    async def fake_apply_task_action(_session, _task, _name, _input):
        return {"outcome": "reopened", "status_change": "open"}

    async def fake_save_notes(*_args):
        return []

    monkeypatch.setattr(thread_runner, "chat", fake_chat)
    monkeypatch.setattr(thread_runner.tasks_store, "latest_status_for", lambda *_args: {task.id: "closed"})
    monkeypatch.setattr(thread_runner, "apply_task_action", fake_apply_task_action)
    monkeypatch.setattr(thread_runner, "save_notes", fake_save_notes)
    monkeypatch.setattr(
        thread_runner.raw_inputs,
        "finalize",
        lambda _session, raw_id, **kwargs: finalized.update(
            {"raw_id": raw_id, **kwargs}
        ),
    )

    trace = await thread_runner.run_thread_followup(
        SimpleNamespace(commit=lambda: None), raw, task
    )

    assert trace["current_task"] == {
        "id": str(task.id),
        "title": "Submit renewal paperwork",
        "description": "Use the current account packet.",
        "due_date": now.isoformat(),
        "estimation": 45,
        "location": "Office",
        "link": "https://example.test/task",
        "label": "Admin",
    }
    assert finalized["agent_trace"]["current_task"] == trace["current_task"]


@pytest.mark.asyncio
async def test_explicit_followup_rejects_no_change(monkeypatch):
    import app.agent.thread.runner as thread_runner

    task = SimpleNamespace(
        id=uuid.uuid4(), title="Submit report", description=None,
        due_date=None, scheduled_date=None, estimation=None,
        location=None, link=None, label=None,
    )
    raw = SimpleNamespace(
        id=uuid.uuid4(), source="gmail", source_metadata={},
        content="Submit the report", task_id=task.id,
        agent_trace={"outcome": "not_task", "manual_override": {"outcome": "processing"}},
    )
    captured = {}

    async def fake_chat(_messages, _settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            text="", stop_reason="tool_use", usage={}, provider="test", model="test",
            tool_calls=(ToolCall(id="no-change", name="no_change", input={}),),
        )

    monkeypatch.setattr(thread_runner, "chat", fake_chat)
    monkeypatch.setattr(thread_runner.tasks_store, "latest_status_for", lambda *_args: {task.id: "open"})
    monkeypatch.setattr(thread_runner.labels_store, "agent_descriptions", lambda *_args: {})

    with pytest.raises(RuntimeError, match="did not update"):
        await thread_runner.run_thread_followup(SimpleNamespace(), raw, task, require_change=True)

    assert [tool["name"] for tool in captured["tools"]] == ["update_task"]
    assert captured["force_tool"] == "update_task"
    assert raw.agent_trace["manual_override"]["outcome"] == "processing"


@pytest.mark.asyncio
async def test_reopen_only_applies_due_date_and_open_status(monkeypatch):
    import app.agent.thread.runner as thread_runner

    task = SimpleNamespace(
        id=uuid.uuid4(), title="Submit report", description=None,
        due_date=None, scheduled_date=None, estimation=None,
        location=None, link=None, label=None,
    )
    raw = SimpleNamespace(
        id=uuid.uuid4(), source="manual",
        source_metadata={"action": "reopen_task"},
        content="Reopen task", task_id=None, agent_trace=None,
    )
    applied = []

    async def fake_chat(_messages, _settings, **_kwargs):
        return SimpleNamespace(
            text="", stop_reason="tool_use", usage={}, provider="test", model="test",
            tool_calls=(ToolCall(id="reopen", name="update_task", input={
                "due_date": "2026-12-01T12:00:00+00:00", "title": "Wrong title",
                "location": "Wrong place", "status": "closed",
            }),),
        )

    async def fake_apply(_session, _task, _name, action_input):
        applied.append(action_input)
        return {"outcome": "reopened", "status_change": "open"}

    async def fake_save_notes(*_args):
        return []

    monkeypatch.setattr(thread_runner, "chat", fake_chat)
    monkeypatch.setattr(thread_runner.tasks_store, "latest_status_for", lambda *_args: {task.id: "closed"})
    monkeypatch.setattr(thread_runner, "apply_task_action", fake_apply)
    monkeypatch.setattr(thread_runner.labels_store, "agent_descriptions", lambda *_args: {})
    monkeypatch.setattr(thread_runner, "save_notes", fake_save_notes)
    monkeypatch.setattr(thread_runner.raw_inputs, "finalize", lambda *_args, **_kwargs: None)

    await thread_runner.run_thread_followup(SimpleNamespace(commit=lambda: None), raw, task, require_change=True)
    assert applied == [{
        "due_date": "2026-12-01T12:00:00+00:00", "status": "open",
        "reason": None, "confidence": None,
    }]


@pytest.mark.asyncio
async def test_closed_task_reminder_leaves_anchor_closed(monkeypatch):
    import app.agent.thread.runner as thread_runner

    task = SimpleNamespace(
        id=uuid.uuid4(), title="Submit report", description=None,
        due_date=None, scheduled_date=None, estimation=30,
        location=None, link=None, label=None,
    )
    raw = SimpleNamespace(
        id=uuid.uuid4(), source="gmail", source_metadata={},
        content="Reminder about the report", task_id=None, agent_trace=None,
    )
    finalized = {}

    async def fake_chat(messages, _settings, **_kwargs):
        assert "status: closed" in messages[0].text
        return SimpleNamespace(
            text="", stop_reason="tool_use", usage={}, provider="test", model="test",
            tool_calls=(ToolCall(id="reminder", name="no_change", input={}),),
        )

    async def no_notes(*_args):
        return []

    monkeypatch.setattr(thread_runner, "chat", fake_chat)
    monkeypatch.setattr(thread_runner.tasks_store, "latest_status_for", lambda *_args: {task.id: "closed"})
    monkeypatch.setattr(thread_runner.labels_store, "agent_descriptions", lambda *_args: {})
    monkeypatch.setattr(thread_runner, "save_notes", no_notes)
    monkeypatch.setattr(
        thread_runner.raw_inputs, "finalize",
        lambda _session, _raw_id, **kwargs: finalized.update(kwargs),
    )

    result = await thread_runner.run_thread_followup(SimpleNamespace(commit=lambda: None), raw, task)

    assert result["outcome"] == "no_change"
    assert finalized["status"] == "duplicate"
    assert finalized["task_id"] == task.id
