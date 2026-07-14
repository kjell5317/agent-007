from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import dispatch, text  # noqa: E402
from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.input import runner as input_runner  # noqa: E402


def _response(tool_call: ToolCall) -> LLMResponse:
    return LLMResponse(
        message=LLMMessage(role="assistant", tool_calls=(tool_call,)),
        tool_calls=(tool_call,),
        text="",
        stop_reason="tool_use",
        usage={},
        meta={},
        provider="test",
        model="test",
    )


def test_now_iso_ceil_rounds_current_time_to_five_minutes(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 7, 1, 12, 2, 30, tzinfo=timezone.utc)
            return current.astimezone(tz) if tz else current

    monkeypatch.setattr(text, "datetime", FixedDateTime)

    assert text.now_iso("UTC") == "2026-07-01T12:05:00+00:00"


def test_normalize_agent_due_date_ceil_rounds_to_five_minutes():
    assert (
        text.normalize_agent_due_date("2026-07-01T12:00:00+00:00").isoformat()
        == "2026-07-01T12:00:00+00:00"
    )
    assert (
        text.normalize_agent_due_date("2026-07-01T12:01:10+00:00").isoformat()
        == "2026-07-01T12:05:00+00:00"
    )
    assert (
        text.normalize_agent_due_date("2026-07-01T23:59:59+00:00").isoformat()
        == "2026-07-02T00:00:00+00:00"
    )


@pytest.mark.asyncio
async def test_new_input_create_task_normalizes_agent_due_date(monkeypatch):
    task_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    created_payloads = []
    finalized = {}

    async def fake_chat(*_args, **_kwargs):
        return _response(
            ToolCall(
                id="create-1",
                name="create_task",
                input={
                    "title": "Send report",
                    "estimation": 30,
                    "due_date": "2026-07-02T09:01:10+00:00",
                    "label": "admin",
                    "reason": "explicit deadline in the email",
                    "confidence": 0.85,
                },
            )
        )

    def fake_create(_session, payload):
        created_payloads.append(payload)
        return SimpleNamespace(id=task_id, title=payload.title)

    async def fake_schedule_task(_session, _task):
        return None

    def fake_finalize(_session, raw_id, **kwargs):
        finalized.update({"raw_id": raw_id, **kwargs})

    monkeypatch.setattr(input_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(input_runner, "chat", fake_chat)
    monkeypatch.setattr(input_runner.tasks, "create", fake_create)
    monkeypatch.setattr(input_runner, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(input_runner.raw_inputs, "finalize", fake_finalize)

    raw = SimpleNamespace(
        id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
        source="manual",
        source_metadata={},
        content="Send the report.",
    )
    session = SimpleNamespace(commit=lambda: None)

    trace = await input_runner.run_new_input_agent(session, raw, [], None)

    assert trace["outcome"] == "task_created"
    assert created_payloads[0].due_date.isoformat() == "2026-07-02T09:05:00+00:00"
    assert finalized["status"] == "open"
    assert finalized["task_id"] == task_id
    # create_task now explains itself: reason/confidence land on the trace (which
    # the inbox surfaces in the Agent-trace dropdown), not on the created task.
    assert trace["reason"] == "explicit deadline in the email"
    assert trace["confidence"] == 0.85
    assert not hasattr(created_payloads[0], "reason")


@pytest.mark.asyncio
async def test_update_task_action_normalizes_agent_due_date(monkeypatch):
    patches = []

    async def fake_update_task_svc(_session, _task_id, patch):
        patches.append(patch)

    monkeypatch.setattr(dispatch, "update_task_svc", fake_update_task_svc)

    task = SimpleNamespace(id=uuid.UUID("30000000-0000-0000-0000-000000000001"))
    result = await dispatch.apply_task_action(
        SimpleNamespace(),
        task,
        "update_task",
        {"due_date": "2026-07-02T09:01:00+00:00"},
    )

    # reason/confidence ride along on every frag; None when the agent omits them.
    assert result == {
        "outcome": "updated",
        "status_change": None,
        "reason": None,
        "confidence": None,
    }
    assert patches[0]["due_date"].isoformat() == "2026-07-02T09:05:00+00:00"


@pytest.mark.asyncio
async def test_reopen_action_reopens_then_applies_due_date(monkeypatch):
    task_id = uuid.UUID("30000000-0000-0000-0000-000000000002")
    calls = []
    patches = []

    async def fake_reopen_task_svc(_session, reopened_task_id):
        calls.append(("reopen", reopened_task_id))

    async def fake_update_task_svc(_session, updated_task_id, patch):
        calls.append(("update", updated_task_id))
        patches.append(patch)

    monkeypatch.setattr(dispatch, "reopen_task_svc", fake_reopen_task_svc)
    monkeypatch.setattr(dispatch, "update_task_svc", fake_update_task_svc)

    task = SimpleNamespace(id=task_id)
    result = await dispatch.apply_task_action(
        SimpleNamespace(),
        task,
        "update_task",
        {
            "status": "open",
            "due_date": "2026-07-04T10:01:00+00:00",
        },
    )

    assert result == {
        "outcome": "reopened",
        "status_change": "open",
        "reason": None,
        "confidence": None,
    }
    assert calls == [("reopen", task_id), ("update", task_id)]
    assert patches == [{"due_date": datetime(2026, 7, 4, 10, 5, tzinfo=timezone.utc)}]


@pytest.mark.asyncio
async def test_task_action_frag_carries_reason_and_confidence():
    # The agent's rationale rides the frag so the trace (and its UI dropdown) can
    # explain a reopen/update the same way mark_not_task already does.
    result = await dispatch.apply_task_action(
        SimpleNamespace(),
        SimpleNamespace(id=uuid.UUID("30000000-0000-0000-0000-000000000003")),
        "no_change",
        {"existing_task_id": "x", "reason": "same email again", "confidence": 0.9},
    )
    assert result["reason"] == "same email again"
    assert result["confidence"] == 0.9
