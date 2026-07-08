"""Chat runner loop, DB-free: mock retrieval + LLM, assert the emitted event
sequence, citation tagging, and tool dispatch wiring."""

from __future__ import annotations

import pytest

from app.agent.chat import runner as chat_runner
from app.agent.chat.runner import ChatTurn, Citations, run_chat
from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall
from app.config import get_settings
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


async def _noop_emit(event, data):
    return None


@pytest.mark.asyncio
async def test_search_source_calendar_hits_calendar_api_only(monkeypatch):
    called = {"cal": 0, "local": 0, "drive": 0}

    async def fake_cal(session, *, time_min, time_max, query):
        called["cal"] += 1
        assert time_min == "2026-07-01" and query == "dentist"
        return "Existing calendar events:\n- id=ev1 | 2026-07-02T09:00 | Dentist"

    async def fake_local(session, query, *, limit, filters=None):
        called["local"] += 1
        return []

    async def fake_drive(session, query, *, k, timeout):
        called["drive"] += 1
        return []

    monkeypatch.setattr(chat_runner, "run_find_calendar_events", fake_cal)
    monkeypatch.setattr(chat_runner, "retrieve_local", fake_local)
    monkeypatch.setattr(chat_runner, "search_drive", fake_drive)

    out, trace = await chat_runner._search(
        object(),
        Citations(),
        {"query": "dentist", "source": "calendar", "after": "2026-07-01"},
        get_settings(),
        _noop_emit,
    )
    assert called == {"cal": 1, "local": 0, "drive": 0}
    assert "id=ev1" in out
    assert trace["purpose"] == "calendar search"


@pytest.mark.asyncio
async def test_search_source_drive_hits_drive_only(monkeypatch):
    called = {"cal": 0, "local": 0, "drive": 0}

    async def fake_cal(session, *, time_min, time_max, query):
        called["cal"] += 1
        return ""

    async def fake_local(session, query, *, limit, filters=None):
        called["local"] += 1
        return []

    async def fake_drive(session, query, *, k, timeout):
        called["drive"] += 1
        return [_hit("drive", "f1", "Budget.xlsx", url="http://x")]

    monkeypatch.setattr(chat_runner, "run_find_calendar_events", fake_cal)
    monkeypatch.setattr(chat_runner, "retrieve_local", fake_local)
    monkeypatch.setattr(chat_runner, "search_drive", fake_drive)

    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    out, trace = await chat_runner._search(
        object(), Citations(), {"query": "budget", "source": "drive"}, get_settings(), emit
    )
    assert called == {"cal": 0, "local": 0, "drive": 1}
    assert any(e == "citations" for e, _ in events)
    assert events[0][1]["items"][0]["tag"] == "D1"


@pytest.mark.asyncio
async def test_update_event_delete_routes_to_delete(monkeypatch):
    called = {"del": 0, "upd": 0}

    async def fake_del(session, *, event_id):
        called["del"] += 1
        return "delete_event: deleted 'Standup'.", event_id

    async def fake_upd(session, **kwargs):
        called["upd"] += 1
        return "update_event: updated.", "e1"

    monkeypatch.setattr(chat_runner, "run_delete_event", fake_del)
    monkeypatch.setattr(chat_runner, "run_update_event", fake_upd)

    _, trace = await chat_runner._dispatch(
        object(),
        Citations(),
        ToolCall(id="1", name="update_event", input={"event_id": "e1", "delete": True}),
        get_settings(),
        _noop_emit,
    )
    assert called == {"del": 1, "upd": 0}
    assert trace["purpose"] == "delete event"
    assert trace["changed_state"] is True


@pytest.mark.asyncio
async def test_search_no_source_federates_local_and_drive(monkeypatch):
    called = {"local": 0, "drive": 0}

    async def fake_local(session, query, *, limit, filters=None):
        called["local"] += 1
        return [_hit("task", "t1", "Task")]

    async def fake_drive(query, *, k, timeout):
        called["drive"] += 1
        return [_hit("drive", "d1", "Doc", url="http://x")]

    monkeypatch.setattr(chat_runner, "retrieve_local", fake_local)
    monkeypatch.setattr(chat_runner, "_retrieve_drive", fake_drive)

    out, trace = await chat_runner._search(
        object(), Citations(), {"query": "stuff"}, get_settings(), _noop_emit
    )
    assert called == {"local": 1, "drive": 1}
