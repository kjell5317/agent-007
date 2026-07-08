"""Chat runner loop, DB-free: mock retrieval + LLM, assert the emitted event
sequence, citation tagging, and tool dispatch wiring."""

from __future__ import annotations

import pytest

from app.agent.chat import runner as chat_runner
from app.agent.chat.runner import ChatTurn, Citations, run_chat
from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall
from app.db.schemas.search import SearchHit


def _hit(type_: str, id_: str, title: str, **kw) -> SearchHit:
    return SearchHit(type=type_, id=id_, title=title, score=1.0, **kw)


def _resp(text: str = "", tool_calls: tuple[ToolCall, ...] = ()) -> LLMResponse:
    msg = LLMMessage(role="assistant", text=text or None, tool_calls=tool_calls)
    return LLMResponse(
        message=msg,
        tool_calls=tool_calls,
        text=text,
        stop_reason="end_turn" if not tool_calls else "tool_use",
        usage={},
        meta={},
        provider="test",
        model="test",
    )


def test_citations_tagging_dedupes_and_prefixes_by_type():
    cites = Citations()
    first = cites.add(
        [
            _hit("task", "t1", "Task one"),
            _hit("input", "i1", "Input one"),
            _hit("task", "t2", "Task two"),
        ]
    )
    assert [tag for tag, _ in first] == ["T1", "I1", "T2"]
    # Re-adding the same hits assigns no new tags (dedupe by type+id).
    again = cites.add([_hit("task", "t1", "Task one"), _hit("drive", "d1", "Doc")])
    assert [tag for tag, _ in again] == ["D1"]


@pytest.mark.asyncio
async def test_run_chat_streams_citations_tools_and_tokens(monkeypatch):
    async def fake_drive(query, *, k, timeout):
        return []

    # The model first calls `search` (with metadata filters), then answers.
    scripted = [
        _resp(
            tool_calls=(
                ToolCall(
                    id="1",
                    name="search",
                    input={"query": "groceries", "source": "Gmail", "status": "open"},
                ),
            )
        ),
        _resp(text="You have one open task [T1]."),
    ]

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        resp = scripted.pop(0)
        if resp.text:
            await on_delta(resp.text)
        return resp

    monkeypatch.setattr(chat_runner, "_retrieve_drive", fake_drive)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)

    # First call = initial retrieval (no filters); second = the `search` tool,
    # whose Filters we capture to assert the tool forwards metadata filters.
    calls: dict = {"n": 0, "tool_filters": None}

    async def local_router(session, query, *, limit, filters=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [_hit("task", "abc", "Buy milk", task_id="abc", status="open")]
        calls["tool_filters"] = filters
        return [_hit("input", "raw1", "Grocery email", task_id=None)]

    monkeypatch.setattr(chat_runner, "retrieve_local", local_router)

    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    await run_chat(object(), [ChatTurn(role="user", content="what are my tasks?")], emit=emit)

    kinds = [e for e, _ in events]
    assert kinds[0] == "citations"
    assert kinds[-1] == "done"
    assert "tool_call" in kinds
    assert "token" in kinds

    # Initial citation carries the tagged local hit.
    first_items = events[0][1]["items"]
    assert first_items[0]["tag"] == "T1"
    assert first_items[0]["title"] == "Buy milk"

    # The search tool emitted a second citations event with a fresh tag.
    tool_events = [d for e, d in events if e == "tool_call"]
    assert tool_events and tool_events[0]["name"] == "search"

    # Metadata filters were forwarded (source lower-cased, status passed through).
    assert calls["tool_filters"] is not None
    assert calls["tool_filters"].source == "gmail"
    assert calls["tool_filters"].status == "open"

    # The answer text was streamed.
    tokens = "".join(d["text"] for e, d in events if e == "token")
    assert "one open task" in tokens
