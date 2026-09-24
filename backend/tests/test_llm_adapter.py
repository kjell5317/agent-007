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


def test_settings_default_buffers():
    settings = Settings(database_url="postgresql+psycopg://test:test@localhost/test")

    assert settings.commute_event_buffer_minutes == 5
    assert settings.event_buffer_minutes == 15


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

    monkeypatch.setattr(llm, "_build_generator", lambda settings, **kw: FakeGenerator())
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
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "search_notes",
            "input": {"query": "project alpha"},
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_sdk_request_omits_removed_sampling_parameters(monkeypatch, streaming):
    generator = llm.AnthropicChatGenerator(
        api_key=llm.Secret.from_token("test-key"), model="claude-test"
    )
    captured = {}

    # Deliberately match the supported request arguments without **kwargs:
    # removed parameters must fail here, just as they do in the newer SDK.
    async def create(*, model, messages, system, tools, stream, max_tokens, tool_choice=None):
        captured.update(
            model=model, messages=messages, system=system, tools=tools,
            stream=stream, max_tokens=max_tokens, tool_choice=tool_choice,
        )
        return "response"

    async def process(response, callback):
        assert response == "response"
        if callback:
            await callback(llm.StreamingChunk(content="created"))
        return {"replies": [ChatMessage.from_assistant(text="created")]}

    monkeypatch.setattr(generator.async_client.messages, "create", create)
    monkeypatch.setattr(generator, "_process_response_async", process)
    monkeypatch.setattr(llm, "_build_generator", lambda settings, **kwargs: generator)
    settings = SimpleNamespace(
        effective_llm_provider="anthropic", effective_llm_model="claude-test"
    )
    kwargs = {
        "system_prompt": "Extract a task",
        "tools": [{
            "name": "create_task", "description": "Create a task",
            "parameters": {"type": "object", "properties": {}},
        }],
    }
    try:
        if streaming:
            deltas = []

            async def on_delta(text):
                deltas.append(text)

            response = await llm.stream_chat(
                [llm.user_message("test")], settings, on_delta=on_delta, **kwargs
            )
            assert deltas == ["created"]
            assert captured["max_tokens"] == 1500
        else:
            response = await llm.chat(
                [llm.user_message("test")], settings, force_tool="create_task", **kwargs
            )
            assert captured["tool_choice"] == {"type": "tool", "name": "create_task"}
            assert captured["max_tokens"] == llm.MAX_TOKENS
        assert response.text == "created"
        assert captured["stream"] is streaming
        assert captured["tools"][0]["name"] == "create_task"
    finally:
        await generator.async_client.close()
        generator.client.close()


def test_google_keeps_temperature():
    assert llm._generation_kwargs("google", 1024)["temperature"] == llm.TEMPERATURE
