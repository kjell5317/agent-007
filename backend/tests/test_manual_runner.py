from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.manual import runner  # noqa: E402


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
