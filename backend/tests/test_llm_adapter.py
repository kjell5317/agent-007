from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from haystack.dataclasses import ChatMessage, ToolCall as HaystackToolCall

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import llm  # noqa: E402
from app.config.settings import DEFAULT_ANTHROPIC_MODEL, Settings  # noqa: E402


def test_settings_prefers_llm_model_and_keeps_claude_model_fallback():
    configured = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        llm_provider="Anthropic",
        llm_model="claude-sonnet-4-5",
        claude_model="legacy-claude",
    )
    assert configured.effective_llm_provider == "anthropic"
    assert configured.effective_llm_model == "claude-sonnet-4-5"

    legacy = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        llm_model="",
        claude_model="legacy-claude",
    )
    assert legacy.effective_llm_model == "legacy-claude"

    defaulted = Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        llm_provider="",
        llm_model="",
        claude_model="",
    )
    assert defaulted.effective_llm_provider == "anthropic"
    assert defaulted.effective_llm_model == DEFAULT_ANTHROPIC_MODEL


@pytest.mark.asyncio
async def test_chat_normalizes_tools_messages_and_response(monkeypatch):
    captured = {}

    class FakeGenerator:
        async def run_async(self, **kwargs):
            captured.update(kwargs)
            return {
                "replies": [
                    ChatMessage.from_assistant(
                        text="checking",
                        meta={
                            "finish_reason": "tool_use",
                            "usage": {"input_tokens": 12, "output_tokens": 5},
                        },
                        tool_calls=[
                            HaystackToolCall(
                                tool_name="search_notes",
                                arguments={"query": "project alpha"},
                                id="call-1",
                            )
                        ],
                    )
                ]
            }

    monkeypatch.setattr(llm, "_build_generator", lambda settings: FakeGenerator())
    settings = SimpleNamespace(
        effective_llm_provider="anthropic",
        effective_llm_model="claude-test",
    )
    prior_call = llm.ToolCall(id="call-0", name="search_notes", input={"query": "alpha"})

    response = await llm.chat(
        [
            llm.user_message("hello"),
            llm.LLMMessage(role="assistant", tool_calls=(prior_call,)),
            llm.tool_result_message(prior_call, "Notes:\n- alpha"),
        ],
        settings,
        system_prompt="system prompt",
        tools=[
            {
                "name": "search_notes",
                "description": "Search saved notes.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ],
        force_tool="search_notes",
    )

    assert [message.role.value for message in captured["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert captured["generation_kwargs"]["tool_choice"] == {
        "type": "tool",
        "name": "search_notes",
    }
    # System message carries the ephemeral cache breakpoint so Anthropic caches
    # the tools → system prefix across iterations and runs.
    system_message = captured["messages"][0]
    assert system_message.meta["cache_control"] == {"type": "ephemeral"}
    assert captured["tools"][0].name == "search_notes"
    assert captured["tools"][0].parameters["required"] == ["query"]
    assert response.text == "checking"
    assert response.stop_reason == "tool_use"
    assert response.usage == {"input_tokens": 12, "output_tokens": 5}
    assert response.tool_calls == (
        llm.ToolCall(id="call-1", name="search_notes", input={"query": "project alpha"}),
    )
    assert llm.block_summary(response) == [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "name": "search_notes", "input": {"query": "project alpha"}},
    ]
