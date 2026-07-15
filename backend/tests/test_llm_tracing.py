"""LLM boundary instrumentation: the pure trace-payload mappers, and that
`chat()` opens a generation observation and feeds it model + token usage +
output (with a fake generator so no provider call happens)."""

from __future__ import annotations

import contextlib

import pytest
from haystack.dataclasses import ChatMessage

from app.agent.helpers import llm
from app.agent.helpers.llm import (
    LLMMessage,
    LLMResponse,
    ToolCall,
    _obs_input,
    _obs_output,
    _usage_details,
    user_message,
)
from app.config import get_settings


def _resp(text="hello", tool_calls=(), usage=None) -> LLMResponse:
    return LLMResponse(
        message=LLMMessage(role="assistant", text=text or None, tool_calls=tool_calls),
        tool_calls=tool_calls,
        text=text,
        stop_reason="end_turn",
        usage=usage or {},
        meta={},
        provider="anthropic",
        model="claude-x",
    )


def test_usage_details_maps_and_dedupes():
    assert _usage_details({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}) == {
        "input": 10,
        "output": 4,
        "total": 14,
    }
    # Anthropic-style keys map to the same targets; first-seen wins on dupes.
    assert _usage_details({"input_tokens": 3, "output_tokens": 5}) == {"input": 3, "output": 5}
    assert _usage_details({"prompt_tokens": None, "foo": "bar"}) == {}


def test_obs_input_includes_system_and_messages():
    tc = ToolCall(id="1", name="search", input={"q": "x"})
    msgs = [
        user_message("hi"),
        LLMMessage(role="assistant", text="ok", tool_calls=(tc,)),
    ]
    out = _obs_input("SYS", msgs)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert out[1] == {"role": "user", "content": "hi"}
    assert out[2]["tool_calls"] == [{"name": "search", "input": {"q": "x"}}]


def test_obs_output_captures_text_and_tool_calls():
    tc = ToolCall(id="1", name="create_task", input={"title": "t"})
    assert _obs_output(_resp(text="done", tool_calls=(tc,))) == {
        "text": "done",
        "tool_calls": [{"name": "create_task", "input": {"title": "t"}}],
    }


@pytest.mark.asyncio
async def test_chat_records_generation_with_model_and_usage(monkeypatch):
    captured: dict = {}

    @contextlib.contextmanager
    def fake_generation(name, *, model=None, input=None, metadata=None):
        captured.update(name=name, model=model, input=input, in_metadata=metadata)

        class _Gen:
            def update(self, **kw):
                captured["update"] = kw

        yield _Gen()

    class _FakeGenerator:
        async def run_async(self, **kwargs):
            reply = ChatMessage.from_assistant(
                "hello",
                meta={
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                    "finish_reason": "end_turn",
                },
            )
            return {"replies": [reply]}

    monkeypatch.setattr(llm.obs, "generation", fake_generation)
    monkeypatch.setattr(llm, "_build_generator", lambda settings, **kw: _FakeGenerator())

    resp = await llm.chat(
        [user_message("hi")],
        get_settings(),
        system_prompt="sys",
        tools=[{"name": "search"}],
        name="chat-turn",
    )

    assert resp.text == "hello"
    assert captured["name"] == "chat-turn"
    assert captured["model"] == get_settings().effective_llm_model
    assert captured["in_metadata"]["tools"] == ["search"]
    assert captured["update"]["usage_details"] == {"input": 3, "output": 4}
    assert captured["update"]["output"] == {"text": "hello"}
    assert captured["update"]["metadata"] == {"stop_reason": "end_turn"}
