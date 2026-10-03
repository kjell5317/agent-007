from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent import orchestrator  # noqa: E402
from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.input import runner as input_runner  # noqa: E402
from app.agent.thread import runner  # noqa: E402
from app.db.clients.raw_inputs import SimilarInput  # noqa: E402


def _task(task_id: uuid.UUID, title: str, *, parent_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=task_id, title=title, parent_task_id=parent_id,
        is_container=parent_id is None, description=None, due_date=None,
        scheduled_date=None, estimation=30, location=None, link=None, label=None,
    )


@pytest.mark.asyncio
async def test_thread_followup_shows_all_subtasks_and_closes_selected_ones(monkeypatch):
    parent = _task(uuid.uuid4(), "Prepare launch")
    children = [
        _task(uuid.uuid4(), "Draft announcement", parent_id=parent.id),
        _task(uuid.uuid4(), "Send announcement", parent_id=parent.id),
    ]
    raw = SimpleNamespace(
        id=uuid.uuid4(), source="gmail", source_metadata={},
        content="All launch work is finished.", task_id=None,
    )
    closed = []
    finalized = {}

    async def fake_chat(messages, _settings, **kwargs):
        prompt = messages[0].text
        assert str(parent.id) not in prompt
        assert all(str(child.id) in prompt for child in children)
        assert "status: open" in prompt
        close_tool = next(tool for tool in kwargs["tools"] if tool["name"] == "close_subtasks")
        assert set(close_tool["parameters"]["properties"]["task_ids"]["items"]["enum"]) == {
            str(child.id) for child in children
        }
        call = ToolCall(
            id="close-children", name="close_subtasks",
            input={"task_ids": [str(child.id) for child in children]},
        )
        return LLMResponse(
            message=LLMMessage(role="assistant", tool_calls=(call,)),
            tool_calls=(call,), text="", stop_reason="tool_use", usage={}, meta={},
            provider="test", model="test",
        )

    async def fake_action(_session, task, name, payload):
        assert name == "update_task"
        assert payload == {"status": "closed"}
        closed.append(task.id)
        return {"outcome": "closed"}

    async def no_notes(*_args):
        return []

    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(runner, "chat", fake_chat)
    monkeypatch.setattr(runner, "apply_task_action", fake_action)
    monkeypatch.setattr(runner, "save_notes", no_notes)
    monkeypatch.setattr(
        runner.tasks_store, "latest_status_for",
        lambda _session, ids: {task_id: "open" for task_id in ids},
    )
    monkeypatch.setattr(
        runner.raw_inputs, "finalize",
        lambda _session, _raw_id, **kwargs: finalized.update(kwargs),
    )

    trace = await runner.run_thread_followup(
        SimpleNamespace(commit=lambda: None), raw, parent, subtasks=children
    )

    assert closed == [child.id for child in children]
    assert finalized["task_id"] == parent.id
    assert trace["closed_subtask_ids"] == [str(child.id) for child in children]


@pytest.mark.asyncio
async def test_thread_linked_subtask_routes_with_all_siblings(monkeypatch):
    parent = _task(uuid.uuid4(), "Prepare launch")
    children = [
        _task(uuid.uuid4(), "Draft announcement", parent_id=parent.id),
        _task(uuid.uuid4(), "Send announcement", parent_id=parent.id),
    ]
    raw = SimpleNamespace(
        id=uuid.uuid4(), source="gmail", source_metadata={"thread_id": "launch"},
        processed_at=None,
    )
    linked = SimpleNamespace(id=uuid.uuid4(), task_id=children[0].id)
    by_id = {task.id: task for task in [parent, *children]}
    captured = {}

    async def no_shortcut(*_args):
        return None

    async def fake_followup(_session, _raw, task, **kwargs):
        captured.update(task=task, **kwargs)
        return {"outcome": "no_change"}

    monkeypatch.setattr(orchestrator.raw_inputs, "get", lambda *_args: raw)
    monkeypatch.setattr(orchestrator.raw_inputs, "find_by_thread", lambda *_args, **_kwargs: linked)
    monkeypatch.setattr(orchestrator.tasks, "get", lambda _session, task_id: by_id.get(task_id))
    monkeypatch.setattr(orchestrator.tasks, "children", lambda _session, _id: children)
    monkeypatch.setattr(orchestrator, "_github_email_shortcut", no_shortcut)
    monkeypatch.setattr(orchestrator, "run_thread_followup", fake_followup)

    result = await orchestrator._process_raw_input(SimpleNamespace(), raw.id)

    assert result["outcome"] == "no_change"
    assert captured == {"task": parent, "subtasks": children}


@pytest.mark.asyncio
async def test_new_input_context_omits_parent_candidate(monkeypatch):
    parent = _task(uuid.uuid4(), "Prepare launch")
    child = _task(uuid.uuid4(), "Draft announcement", parent_id=parent.id)
    raw = SimpleNamespace(
        id=uuid.uuid4(), source="gmail", source_metadata={},
        content="Launch update", task_id=None,
    )

    def hit(task):
        return SimilarInput(
            id=uuid.uuid4(), source="gmail", status="open", task_id=task.id,
            similarity=0.8, decayed_similarity=0.8, agent_trace=None,
            subject=task.title, sender=None, content_snippet=task.title,
            received_at=datetime.now(timezone.utc),
        )

    async def fake_chat(messages, _settings, **_kwargs):
        prompt = messages[0].text
        assert str(parent.id) not in prompt
        assert str(child.id) in prompt
        call = ToolCall(id="not-task", name="mark_not_task", input={"reason": "FYI"})
        return LLMResponse(
            message=LLMMessage(role="assistant", tool_calls=(call,)),
            tool_calls=(call,), text="", stop_reason="tool_use", usage={}, meta={},
            provider="test", model="test",
        )

    async def no_notes(*_args):
        return []

    monkeypatch.setattr(input_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(input_runner, "chat", fake_chat)
    monkeypatch.setattr(input_runner, "save_notes", no_notes)
    monkeypatch.setattr(
        input_runner.tasks, "get",
        lambda _session, task_id: {parent.id: parent, child.id: child}.get(task_id),
    )
    monkeypatch.setattr(input_runner.raw_inputs, "finalize", lambda *_args, **_kwargs: None)

    trace = await input_runner.run_new_input_agent(
        SimpleNamespace(commit=lambda: None), raw, [hit(parent), hit(child)], None
    )

    assert trace["outcome"] == "not_task"
