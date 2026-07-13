"""Chat runner loop, DB-free: mock retrieval + LLM, assert the emitted event
sequence, citation tagging, tool dispatch, and the consolidated event tool."""

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


async def _noop_emit(event, data):
    return None


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
    again = cites.add([_hit("task", "t1", "Task one"), _hit("drive", "d1", "Doc")])
    assert [tag for tag, _ in again] == ["G1"]
    # Documents (kotx/GitHub issues) get "D"; calendar events get "E" — so a
    # document is never read as an event.
    typed = cites.add([_hit("document", "doc1", "Issue"), _hit("calendar", "cal1", "Standup")])
    assert [tag for tag, _ in typed] == ["D1", "E1"]


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("rent invoice", "sources"),
        ("project alpha", "sources"),
        ("uni deadline", "sources"),
        ("what is due today?", "answer"),
        ("create a task to call Alice tomorrow", "answer"),
        ("today tasks", "answer"),
    ],
)
def test_classify_response_mode_examples(text, mode):
    assert chat_runner.classify_response_mode(text) == mode


@pytest.mark.asyncio
async def test_run_chat_streams_citations_tools_and_tokens(monkeypatch):
    # Pre-injection loads tasks + notes; the model then drills into messages via
    # `messages_search` and answers. Capture the tool args to prove they thread.
    scripted = [
        _resp(
            tool_calls=(
                ToolCall(
                    id="1",
                    name="messages_search",
                    input={"query": "groceries", "source": "Gmail", "after": "2026-07-01"},
                ),
            )
        ),
        _resp(text="You have one open task [T1] and a grocery email [I1]."),
    ]

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        resp = scripted.pop(0)
        if resp.text:
            await on_delta(resp.text)
        return resp

    tool_args = {}

    async def fake_retrieve(session, query):
        return [_hit("task", "abc", "Buy milk", task_id="abc", status="open")]

    async def fake_search_messages(session, query, *, source=None, before=None, after=None):
        tool_args.update(query=query, source=source, before=before, after=after)
        return [_hit("input", "raw1", "Grocery email", task_id=None, source="gmail")]

    monkeypatch.setattr(chat_runner, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_runner, "search_messages", fake_search_messages)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)
    monkeypatch.setattr(chat_runner.notion_mcp, "is_connected", lambda _s: False)
    monkeypatch.setattr(chat_runner.github, "is_connected", lambda: False)

    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    await run_chat(object(), [ChatTurn(role="user", content="what are my tasks?")], emit=emit)

    kinds = [e for e, _ in events]
    assert kinds[0] == "citations"
    assert kinds[1] == "response_mode"
    assert kinds[-1] == "done"
    assert "tool_call" in kinds
    assert "token" in kinds

    assert events[0][1]["items"][0]["tag"] == "T1"
    assert events[0][1]["items"][0]["title"] == "Buy milk"
    assert events[1][1]["response_mode"] == "answer"

    tool_events = [d for e, d in events if e == "tool_call"]
    assert tool_events and tool_events[0]["name"] == "messages_search"

    # Tool args forwarded (source lower-cased by the dispatcher).
    assert tool_args == {
        "query": "groceries",
        "source": "gmail",
        "before": None,
        "after": "2026-07-01",
    }

    tokens = "".join(d["text"] for e, d in events if e == "token")
    assert "one open task" in tokens


@pytest.mark.asyncio
async def test_source_mode_emits_metadata_and_prompt_context(monkeypatch):
    seen = {"latest_user": ""}

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        seen["latest_user"] = messages[-1].text
        await on_delta("Rent invoice is the strongest match [I1].")
        return _resp(text="Rent invoice is the strongest match [I1].")

    async def fake_retrieve(session, query, *, filters=None):
        return [_hit("input", "raw1", "Rent invoice", snippet="July rent invoice")]

    monkeypatch.setattr(chat_runner, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)
    monkeypatch.setattr(chat_runner.notion_mcp, "is_connected", lambda _s: False)

    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    await run_chat(object(), [ChatTurn(role="user", content="rent invoice")], emit=emit)

    assert events[0][0] == "citations"
    assert events[1] == ("response_mode", {"response_mode": "sources"})
    assert events[0][1]["items"][0]["tag"] == "I1"
    assert "Response mode: sources" in seen["latest_user"]
    # Sources are agent-curated: one card per cited item, in cited order.
    assert "in the order you cite it" in seen["latest_user"]


def test_context_line_surfaces_action_ids():
    # The uniform record exposes the source_id as `id=` — the value get_drive_file
    # / update_event consume — for every source, replacing the old per-type tags.
    drive = chat_runner._context_line(
        "G1", _hit("drive", "real-file-id", "Pitch", source="drive", status="drive")
    )
    assert "[G1] drive" in drive and "id=real-file-id" in drive
    cal = chat_runner._context_line(
        "E1", _hit("document", "ev123", "Standup", source="calendar", status="event")
    )
    assert "[E1]" in cal and "id=ev123" in cal


def test_context_line_shows_similarity_and_linked_task():
    hit = _hit(
        "note", "n1", "VAT id is DE123", source="note", status="note",
        task_id="task-9", meta={"similarity": 0.82},
    )
    line = chat_runner._context_line("N1", hit)
    assert "sim=0.82" in line and "id=n1" in line and "task=task-9" in line


@pytest.mark.asyncio
async def test_forces_final_answer_when_tool_loop_exhausts(monkeypatch):
    max_iter = get_settings().search_chat_max_iterations
    calls = {"tool_turns": 0, "toolless": 0}

    async def fake_retrieve(session, query, *, filters=None):
        return []

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        # The forced final turn is called with no tools — answer instead of loop.
        if not tools:
            calls["toolless"] += 1
            await on_delta("Couldn't read that file.")
            return _resp(text="Couldn't read that file.")
        calls["tool_turns"] += 1
        return _resp(
            tool_calls=(
                ToolCall(id=str(calls["tool_turns"]), name="get_drive_file", input={"file_id": "x"}),
            )
        )

    async def fake_get_drive_file(session, file_id, *, max_chars):
        return "get_drive_file: couldn't read that file."

    monkeypatch.setattr(chat_runner, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)
    monkeypatch.setattr(chat_runner, "get_drive_file", fake_get_drive_file)
    monkeypatch.setattr(chat_runner.notion_mcp, "is_connected", lambda _s: False)
    monkeypatch.setattr(chat_runner.github, "is_connected", lambda: False)

    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    await run_chat(object(), [ChatTurn(role="user", content="read the deck")], emit=emit)

    assert calls["tool_turns"] == max_iter  # every iteration kept calling tools
    assert calls["toolless"] == 1  # then one forced, tool-less answer
    assert events[-1][0] == "done"
    # A limit indicator is surfaced before the forced answer.
    assert any(e == "tool_call" and d.get("name") == "tool_limit" for e, d in events)
    tokens = "".join(d["text"] for e, d in events if e == "token")
    assert "Couldn't read that file." in tokens


@pytest.mark.asyncio
async def test_notion_tools_exposed_when_connected_and_dispatch(monkeypatch):
    # Connected → the two read-only Notion tools are offered; the model calls
    # notion_search and its result reaches the next turn as a tool message.
    scripted = [
        _resp(tool_calls=(ToolCall(id="1", name="notion_search", input={"query": "roadmap"}),)),
        _resp(text="The roadmap lives in Notion."),
    ]
    seen = {"tool_names": None, "search_query": None}

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        seen["tool_names"] = {t["name"] for t in tools}
        resp = scripted.pop(0)
        if resp.text:
            await on_delta(resp.text)
        return resp

    async def fake_retrieve(session, query, *, filters=None):
        return []

    async def fake_notion_search(session, query):
        seen["search_query"] = query
        return "Notion: 'Roadmap' — https://notion.so/roadmap"

    monkeypatch.setattr(chat_runner, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)
    monkeypatch.setattr(chat_runner.notion_mcp, "is_connected", lambda _s: True)
    monkeypatch.setattr(chat_runner.notion_mcp, "notion_search", fake_notion_search)
    monkeypatch.setattr(chat_runner.github, "is_connected", lambda: False)

    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    await run_chat(object(), [ChatTurn(role="user", content="where is the roadmap?")], emit=emit)

    assert {"notion_search", "notion_fetch"} <= seen["tool_names"]
    assert seen["search_query"] == "roadmap"
    tool_events = [d for e, d in events if e == "tool_call"]
    assert tool_events and tool_events[0]["name"] == "notion_search"
    assert tool_events[0]["status"] == "success"


@pytest.mark.asyncio
async def test_notion_tools_absent_when_disconnected(monkeypatch):
    seen = {"tool_names": set()}

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        seen["tool_names"] = {t["name"] for t in tools}
        await on_delta("no notion")
        return _resp(text="no notion")

    async def fake_retrieve(session, query, *, filters=None):
        return []

    monkeypatch.setattr(chat_runner, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)
    monkeypatch.setattr(chat_runner.notion_mcp, "is_connected", lambda _s: False)
    monkeypatch.setattr(chat_runner.github, "is_connected", lambda: False)

    await run_chat(object(), [ChatTurn(role="user", content="hi")], emit=_noop_emit)

    assert "notion_search" not in seen["tool_names"]
    assert "notion_fetch" not in seen["tool_names"]


@pytest.mark.asyncio
async def test_github_tools_exposed_when_connected_and_dispatch(monkeypatch):
    # Connected → github tools offered; the model calls github_search and its
    # result reaches the next turn. Notion is off to isolate the github path.
    scripted = [
        _resp(tool_calls=(ToolCall(id="1", name="github_search", input={"query": "is:open"}),)),
        _resp(text="You have one open PR."),
    ]
    seen = {"tool_names": None, "query": None}

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        seen["tool_names"] = {t["name"] for t in tools}
        resp = scripted.pop(0)
        if resp.text:
            await on_delta(resp.text)
        return resp

    async def fake_retrieve(session, query, *, filters=None):
        return []

    async def fake_search_issues(query):
        seen["query"] = query
        return "[acme/widgets#7] Fix (issue, open, by me) — https://github.com/acme/widgets/issues/7"

    monkeypatch.setattr(chat_runner, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)
    monkeypatch.setattr(chat_runner.notion_mcp, "is_connected", lambda _s: False)
    monkeypatch.setattr(chat_runner.github, "is_connected", lambda: True)
    monkeypatch.setattr(chat_runner.github, "search_issues", fake_search_issues)

    events: list[tuple[str, dict]] = []

    async def emit(event, data):
        events.append((event, data))

    await run_chat(object(), [ChatTurn(role="user", content="my open github?")], emit=emit)

    assert {"github_search", "github_my_work"} <= seen["tool_names"]
    assert seen["query"] == "is:open"
    tool_events = [d for e, d in events if e == "tool_call"]
    assert tool_events and tool_events[0]["name"] == "github_search"
    assert tool_events[0]["status"] == "success"


@pytest.mark.asyncio
async def test_github_tools_absent_when_disconnected(monkeypatch):
    seen = {"tool_names": set()}

    async def fake_stream(messages, settings, *, system_prompt, tools, on_delta, **kw):
        seen["tool_names"] = {t["name"] for t in tools}
        await on_delta("no gh")
        return _resp(text="no gh")

    async def fake_retrieve(session, query, *, filters=None):
        return []

    monkeypatch.setattr(chat_runner, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_runner, "stream_chat", fake_stream)
    monkeypatch.setattr(chat_runner.notion_mcp, "is_connected", lambda _s: False)
    monkeypatch.setattr(chat_runner.github, "is_connected", lambda: False)

    await run_chat(object(), [ChatTurn(role="user", content="hi")], emit=_noop_emit)

    assert "github_search" not in seen["tool_names"]
    assert "github_my_work" not in seen["tool_names"]


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
async def test_per_source_search_emits_citations_and_uniform_record(monkeypatch):
    async def fake_search_messages(session, query, *, source=None, before=None, after=None):
        return [_hit("input", "raw1", "Grocery email", task_id="abc", source="gmail", sender="Anna")]

    monkeypatch.setattr(chat_runner, "search_messages", fake_search_messages)

    emitted: list[tuple[str, dict]] = []

    async def emit(event, data):
        emitted.append((event, data))

    text, trace = await chat_runner._dispatch(
        object(),
        Citations(),
        ToolCall(id="1", name="messages_search", input={"query": "groceries"}),
        get_settings(),
        emit,
    )
    # A citation is streamed for the hit, and the tool text carries the uniform
    # record: the citation tag, the source_id, and the linked task id.
    assert emitted and emitted[0][0] == "citations"
    assert emitted[0][1]["items"][0]["tag"] == "I1"
    assert trace["name"] == "messages_search"
    assert "[I1]" in text and "id=raw1" in text and "task=abc" in text
