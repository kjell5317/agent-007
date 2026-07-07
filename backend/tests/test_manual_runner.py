from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.manual import runner  # noqa: E402
from app.db.clients.raw_inputs import SimilarInput  # noqa: E402


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


def _hit(**overrides) -> SimilarInput:
    values = {
        "id": "00000000-0000-0000-0000-000000000001",
        "source": "gmail",
        "status": "not_task",
        "task_id": None,
        "label": None,
        "similarity": 0.8123,
        "decayed_similarity": 0.8123,
        "agent_trace": {"reason": "Informational newsletter with no action requested."},
        "subject": "Weekly FYI",
        "sender": "sender@example.com",
        "content_snippet": "Hello,\n\nHere is an FYI update with details for context.",
        "received_at": datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimilarInput(**values)


@pytest.mark.asyncio
async def test_extract_task_fields_forces_create_task_on_final_attempt(monkeypatch):
    calls: list[dict] = []

    async def fake_chat(messages, settings, *, system_prompt, tools, force_tool=None):
        calls.append(
            {
                "messages": messages,
                "tools": [tool["name"] for tool in tools],
                "force_tool": force_tool,
            }
        )
        if len(calls) == 1:
            return _response(
                ToolCall(
                    id="search-1",
                    name="search_notes",
                    input={"query": "quarterly report"},
                )
            )
        return _response(
            ToolCall(
                id="create-1",
                name="create_task",
                input={
                    "title": "Send quarterly report",
                    "estimation": 30,
                    "due_date": "2026-07-02T09:01:00+00:00",
                    "label": "admin",
                },
            )
        )

    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(user_timezone="UTC"),
    )
    async def fake_search_notes(session, query):
        return "Notes:\n- report"

    monkeypatch.setattr(runner, "chat", fake_chat)
    monkeypatch.setattr(runner, "run_search_notes", fake_search_notes)

    raw = SimpleNamespace(
        id="raw-1",
        source="manual",
        source_metadata={},
        content="Please send the quarterly report.",
        received_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    payload, trace = await runner.extract_task_fields(
        SimpleNamespace(), raw, include_trace=True
    )

    assert calls[0]["force_tool"] is None
    assert calls[1]["force_tool"] == "create_task"
    assert calls[0]["tools"] == ["search_notes", "create_task"]
    assert len(calls[1]["messages"]) == 3
    assert calls[1]["messages"][-1].role == "tool"
    assert payload["title"] == "Send quarterly report"
    assert payload["due_date"].isoformat() == "2026-07-02T09:05:00+00:00"
    assert trace["branch"] == "manual"
    assert trace["iterations"][0]["blocks"] == [
        {
            "type": "tool_use",
            "id": "search-1",
            "name": "search_notes",
            "input": {"query": "quarterly report"},
        }
    ]
    assert trace["iterations"][0]["llm"] == {
        "provider": "test",
        "model": "test",
        "usage": {},
    }
    assert trace["iterations"][0]["tool_results"][0]["name"] == "search_notes"
    assert (
        trace["iterations"][0]["tool_results"][0]["result_markdown"]
        == "Notes:\n- report"
    )
    assert trace["iterations"][0]["tool_results"][0]["result_summary"] == "Notes: - report"
    assert trace["iterations"][1]["blocks"] == [
        {
            "type": "tool_use",
            "id": "create-1",
            "name": "create_task",
            "input": {
                "title": "Send quarterly report",
                "estimation": 30,
                "due_date": "2026-07-02T09:01:00+00:00",
                "label": "admin",
            },
        }
    ]
    assert trace["iterations"][1]["tool_results"][0]["name"] == "create_task"


@pytest.mark.asyncio
async def test_extract_task_fields_renders_precedents_and_traces_evidence(monkeypatch):
    captured: dict[str, str] = {}
    task_id = "10000000-0000-0000-0000-000000000001"
    task_hit = _hit(
        id="00000000-0000-0000-0000-000000000002",
        status="open",
        task_id=task_id,
        similarity=0.91,
        subject="Q2 report",
        content_snippet="Please send the Q2 report.",
        label="admin",
    )
    not_task_hit = _hit()
    task = SimpleNamespace(
        id=task_id,
        title="Send Q2 report",
        description="Send the final Q2 report to finance.",
        due_date=None,
        estimation=20,
        location=None,
        link=None,
        label="admin",
    )

    async def fake_chat(messages, settings, *, system_prompt, tools, force_tool=None):
        captured["message"] = messages[0].text
        return _response(
            ToolCall(
                id="create-1",
                name="create_task",
                input={
                    "title": "Send updated Q2 report",
                    "estimation": 30,
                    "due_date": "2026-07-02T09:00:00+00:00",
                    "label": "admin",
                },
            )
        )

    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(runner, "chat", fake_chat)
    monkeypatch.setattr(runner.tasks, "get", lambda _session, _task_id: task)

    raw = SimpleNamespace(
        id="raw-1",
        source="manual",
        source_metadata={"from": "me@example.com"},
        content="Please send the updated Q2 report.",
        received_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    _payload, trace = await runner.extract_task_fields(
        SimpleNamespace(),
        raw,
        precedent_candidates=[task_hit, not_task_hit],
        include_trace=True,
    )

    message = captured["message"]
    assert "Past similar inputs (ranked by similarity)." in message
    assert "[OPEN] sim=0.91 · task_id=10000000-0000-0000-0000-000000000001" in message
    assert "  description: Send the final Q2 report to finance." in message
    assert "  metadata: source=gmail · from=sender@example.com" in message
    assert "  snippet: Please send the Q2 report." in message
    assert "[NOT_TASK] sim=0.81 · title: Weekly FYI" in message
    assert "  reason: Informational newsletter with no action requested." in message
    assert trace["candidates"][0]["ref"] == (
        "candidate:00000000-0000-0000-0000-000000000002"
    )
    assert trace["evidence_refs"][1]["status"] == "not_task"
