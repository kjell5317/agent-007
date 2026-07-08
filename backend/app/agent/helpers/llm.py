"""Provider-neutral Haystack chat adapter shared by every agent flow."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from haystack.dataclasses import StreamingChunk

from haystack.dataclasses import ChatMessage, ToolCall as HaystackToolCall
from haystack.tools import Tool
from haystack.utils import Secret
from haystack_integrations.components.generators.anthropic import AnthropicChatGenerator

from app import observability as obs
from app.config import Settings

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 3
MAX_TOKENS = 1024
TEMPERATURE = 0.4

# Anthropic caches the tools → system prefix up to the first cache breakpoint,
# so a single ephemeral marker on the system message covers both the tool
# schemas and the system prompt — the bulk of every call, re-sent each
# iteration and across runs. Below the model's minimum cacheable size it's a
# silent no-op, so it's safe to always set.
CACHE_CONTROL = {"type": "ephemeral"}

TERMINAL_TOOLS = frozenset({
    "create_task", "mark_not_task",
    "update_task", "no_change",
})

MessageRole = Literal["user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolCall:
    id: str | None
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    tool_call: ToolCall
    content: str
    error: bool = False


@dataclass(frozen=True)
class LLMMessage:
    role: MessageRole
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None


@dataclass(frozen=True)
class LLMResponse:
    message: LLMMessage
    tool_calls: tuple[ToolCall, ...]
    text: str
    stop_reason: str | None
    usage: dict[str, Any]
    meta: dict[str, Any]
    provider: str
    model: str


def user_message(text: str) -> LLMMessage:
    return LLMMessage(role="user", text=text)


def assistant_message(response: LLMResponse) -> LLMMessage:
    return response.message


def tool_result_message(tool_call: ToolCall, content: str) -> LLMMessage:
    return LLMMessage(
        role="tool",
        tool_result=ToolResult(tool_call=tool_call, content=content),
    )


def block_summary(response: LLMResponse) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if response.text:
        blocks.append({"type": "text", "text": response.text})
    blocks.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
        for call in response.tool_calls
    )
    return blocks


async def chat(
    messages: list[LLMMessage],
    settings: Settings,
    *,
    system_prompt: str,
    tools: list[dict[str, Any]],
    force_tool: str | None = None,
    name: str = "llm-call",
) -> LLMResponse:
    """Call the configured Haystack chat backend and normalize its response."""
    provider = settings.effective_llm_provider
    model = settings.effective_llm_model
    generator = _build_generator(settings)
    generation_kwargs: dict[str, Any] = {
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    if force_tool:
        generation_kwargs["tool_choice"] = _tool_choice(provider, force_tool)

    return await _invoke(
        generator,
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        generation_kwargs=generation_kwargs,
        provider=provider,
        model=model,
        name=name,
    )


async def stream_chat(
    messages: list[LLMMessage],
    settings: Settings,
    *,
    system_prompt: str,
    tools: list[dict[str, Any]],
    on_delta: Callable[[str], Awaitable[None]],
    max_tokens: int = 1500,
    name: str = "llm-call",
) -> LLMResponse:
    """Like `chat`, but streams text deltas to `on_delta` as they arrive. The
    provider still assembles the full reply (text + tool calls), which is
    normalized and returned once the stream completes — so the tool loop reads
    tool calls exactly as in the non-streaming path."""
    provider = settings.effective_llm_provider
    model = settings.effective_llm_model
    generator = _build_generator(settings)

    async def _callback(chunk: StreamingChunk) -> None:
        if chunk.content:
            await on_delta(chunk.content)

    return await _invoke(
        generator,
        system_prompt=system_prompt,
        messages=messages,
        tools=tools,
        generation_kwargs={"max_tokens": max_tokens, "temperature": TEMPERATURE},
        provider=provider,
        model=model,
        name=name,
        streaming_callback=_callback,
    )


async def _invoke(
    generator,
    *,
    system_prompt: str,
    messages: list[LLMMessage],
    tools: list[dict[str, Any]],
    generation_kwargs: dict[str, Any],
    provider: str,
    model: str,
    name: str,
    streaming_callback: Callable[[StreamingChunk], Awaitable[None]] | None = None,
) -> LLMResponse:
    """Run one Haystack generation, wrapped in a Langfuse generation observation
    (a no-op when tracing is disabled). Input is set explicitly to the chat
    messages only — never the raw settings/kwargs — so API keys can't leak."""
    run_kwargs: dict[str, Any] = {
        "messages": _to_haystack_messages(system_prompt, messages),
        "tools": [_to_haystack_tool(tool) for tool in tools],
        "generation_kwargs": generation_kwargs,
    }
    if streaming_callback is not None:
        run_kwargs["streaming_callback"] = streaming_callback

    with obs.generation(
        name,
        model=model,
        input=_obs_input(system_prompt, messages),
        metadata={"provider": provider, "tools": [tool["name"] for tool in tools]},
    ) as gen:
        result = await generator.run_async(**run_kwargs)
        replies = result.get("replies") or []
        if not replies:
            raise RuntimeError("LLM provider returned no replies")
        resp = _from_haystack_reply(replies[0], provider=provider, model=model)
        gen.update(
            output=_obs_output(resp),
            usage_details=_usage_details(resp.usage),
            metadata={"stop_reason": resp.stop_reason},
        )
        return resp


def _obs_input(system_prompt: str, messages: list[LLMMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    out.extend(_obs_message(message) for message in messages)
    return out


def _obs_message(message: LLMMessage) -> dict[str, Any]:
    if message.role == "tool" and message.tool_result is not None:
        return {
            "role": "tool",
            "tool": message.tool_result.tool_call.name,
            "content": message.tool_result.content,
        }
    out: dict[str, Any] = {"role": message.role}
    if message.text:
        out["content"] = message.text
    if message.tool_calls:
        out["tool_calls"] = [{"name": c.name, "input": c.input} for c in message.tool_calls]
    return out


def _obs_output(response: LLMResponse) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if response.text:
        out["text"] = response.text
    if response.tool_calls:
        out["tool_calls"] = [{"name": c.name, "input": c.input} for c in response.tool_calls]
    return out


def _usage_details(usage: dict[str, Any]) -> dict[str, int]:
    """Map Haystack/Anthropic token counts onto Langfuse's usage keys (input/
    output/total) so cost is auto-calculated from the model name."""
    out: dict[str, int] = {}
    for src, dst in (
        ("prompt_tokens", "input"),
        ("input_tokens", "input"),
        ("completion_tokens", "output"),
        ("output_tokens", "output"),
        ("total_tokens", "total"),
    ):
        value = usage.get(src)
        if isinstance(value, int) and dst not in out:
            out[dst] = value
    return out


def _build_generator(settings: Settings):
    provider = settings.effective_llm_provider
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for LLM_PROVIDER=anthropic")
        return _anthropic_generator(
            settings.effective_llm_model, settings.anthropic_api_key
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider!r}")


@lru_cache(maxsize=4)
def _anthropic_generator(model: str, api_key: str) -> AnthropicChatGenerator:
    return AnthropicChatGenerator(api_key=Secret.from_token(api_key), model=model)


def _tool_choice(provider: str, tool_name: str) -> dict[str, Any]:
    if provider == "anthropic":
        return {"type": "tool", "name": tool_name}
    return {"type": "function", "function": {"name": tool_name}}


def _to_haystack_messages(system_prompt: str, messages: list[LLMMessage]) -> list[ChatMessage]:
    out = [ChatMessage.from_system(system_prompt, meta={"cache_control": CACHE_CONTROL})]
    out.extend(_to_haystack_message(message) for message in messages)
    return out


def _to_haystack_message(message: LLMMessage) -> ChatMessage:
    if message.role == "user":
        return ChatMessage.from_user(message.text or "")
    if message.role == "assistant":
        return ChatMessage.from_assistant(
            text=message.text,
            tool_calls=[_to_haystack_tool_call(call) for call in message.tool_calls] or None,
        )
    if message.role == "tool":
        if message.tool_result is None:
            raise ValueError("tool message is missing tool_result")
        return ChatMessage.from_tool(
            message.tool_result.content,
            origin=_to_haystack_tool_call(message.tool_result.tool_call),
            error=message.tool_result.error,
        )
    raise ValueError(f"Unsupported message role: {message.role!r}")


def _from_haystack_reply(reply: ChatMessage, *, provider: str, model: str) -> LLMResponse:
    tool_calls = tuple(
        ToolCall(
            id=call.id,
            name=call.tool_name,
            input=dict(call.arguments or {}),
        )
        for call in (reply.tool_calls or [])
    )
    text = "\n".join(reply.texts or [])
    meta = dict(reply.meta or {})
    usage = _extract_usage(meta)
    stop_reason = (
        meta.get("finish_reason")
        or meta.get("stop_reason")
        or meta.get("done_reason")
    )
    message = LLMMessage(
        role="assistant",
        text=text or None,
        tool_calls=tool_calls,
    )
    return LLMResponse(
        message=message,
        tool_calls=tool_calls,
        text=text,
        stop_reason=str(stop_reason) if stop_reason is not None else None,
        usage=usage,
        meta=meta,
        provider=provider,
        model=model,
    )


def _extract_usage(meta: dict[str, Any]) -> dict[str, Any]:
    usage = meta.get("usage") or meta.get("token_usage") or {}
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return {}
    return dict(usage)


def _to_haystack_tool_call(call: ToolCall) -> HaystackToolCall:
    return HaystackToolCall(
        tool_name=call.name,
        arguments=dict(call.input or {}),
        id=call.id,
    )


def _to_haystack_tool(tool: dict[str, Any]) -> Tool:
    return Tool(
        name=str(tool["name"]),
        description=str(tool.get("description") or ""),
        parameters=dict(tool.get("parameters") or {}),
        function=_unused_tool_function,
    )


def _unused_tool_function(**kwargs: Any) -> dict[str, Any]:
    return kwargs
