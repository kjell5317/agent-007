from types import SimpleNamespace
import uuid

import pytest

from app.agent.helpers import web
from app.agent.helpers.llm import ToolCall


@pytest.mark.asyncio
async def test_chat_web_search_returns_grounded_sources(monkeypatch):
    captured = {}
    grounding = SimpleNamespace(
        web_search_queries=["iphone announcement date"],
        grounding_chunks=[SimpleNamespace(web=SimpleNamespace(title="Apple", uri="https://example.com/iphone"))],
    )
    response = SimpleNamespace(
        text="The first iPhone was announced January 9, 2007.",
        candidates=[SimpleNamespace(grounding_metadata=grounding, finish_reason="STOP")],
    )

    class AsyncModels:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return response

    class AsyncClient:
        models = AsyncModels()

        async def aclose(self):
            captured["async_closed"] = True

    class Client:
        aio = AsyncClient()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(web.genai, "Client", lambda **kwargs: Client())
    result = await web.research_web(
        " first iPhone announcement date ",
        SimpleNamespace(gemini_api_key="test", chat_llm_model="gemini-3.5-flash"),
    )
    assert captured["contents"] == "first iPhone announcement date"
    assert len(captured["config"].tools) == 1
    assert captured["config"].tools[0].google_search is not None
    assert "January 9, 2007" in result
    assert "https://example.com/iphone" in result
    assert captured["async_closed"] and captured["closed"]


@pytest.mark.asyncio
async def test_chat_web_search_rejects_unverified_answer(monkeypatch):
    response = SimpleNamespace(
        text="A plausible answer without search",
        candidates=[SimpleNamespace(grounding_metadata=None, finish_reason="STOP")],
    )

    async def _response():
        return response

    async def _close():
        pass

    class Client:
        aio = SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **kwargs: _response()),
            aclose=_close,
        )

        def close(self):
            pass

    monkeypatch.setattr(web.genai, "Client", lambda **kwargs: Client())
    result = await web.research_web(
        "public fact", SimpleNamespace(gemini_api_key="test", chat_llm_model="gemini-3.5-flash")
    )
    assert result.startswith("Web search failed: no grounded search results")


@pytest.mark.asyncio
async def test_task_web_search_respects_flag(monkeypatch):
    async def unexpected_chat(*_args, **_kwargs):
        raise AssertionError("Search must remain disabled")

    monkeypatch.setattr(web, "chat", unexpected_chat)
    assert await web.research_link(
        "https://example.org", SimpleNamespace(task_web_search=False, gemini_api_key="key")
    ) == ("", None)


@pytest.mark.asyncio
async def test_task_web_search_records_query_count(monkeypatch):
    calls = []

    async def fake_chat(messages, *_args, **_kwargs):
        calls.append(messages[0].text)
        return SimpleNamespace(
            text="Useful details",
            provider="google",
            model="gemini-3.5-flash",
            usage={"input_tokens": 10},
            meta={"grounding_metadata": {"web_search_queries": ["one", "two"]}},
        )

    monkeypatch.setattr(web, "chat", fake_chat)
    context, trace = await web.research_link(
        "https://example.org",
        SimpleNamespace(task_web_search=True, gemini_api_key="key", chat_llm_model="gemini-3.5-flash"),
        task_title="Prepare for XY",
        task_context="Prepare slides before the XY meeting",
    )
    assert "Task title: Prepare for XY" in calls[0]
    assert "Task context: Prepare slides before the XY meeting" in calls[0]
    assert context == "Useful details"
    assert trace["search_queries"] == 2
    assert trace["queries"] == ["one", "two"]
    assert trace["result_markdown"] == "Useful details"
    assert trace["search_queries_estimated"] is False


@pytest.mark.asyncio
async def test_task_web_search_omits_incomplete_output(monkeypatch):
    async def fake_chat(*_args, **_kwargs):
        return SimpleNamespace(
            text="Based on the page, here is a summary that ends abruptly",
            stop_reason="MAX_TOKENS",
            provider="google",
            model="test",
            usage={},
            meta={},
        )

    monkeypatch.setattr(web, "chat", fake_chat)
    context, trace = await web.research_link(
        "https://example.org",
        SimpleNamespace(task_web_search=True, gemini_api_key="key", chat_llm_model="test"),
    )
    assert context == ""
    assert trace["status"] == "failed"
    assert trace["result_markdown"] == "Web research returned no complete summary."


def test_concise_result_keeps_complete_lines_only():
    lines = [f"- Fact {i}: " + "x" * 180 for i in range(8)]
    result = web._concise_result("\n".join(lines))
    assert result.splitlines() == lines[:5]
    assert len(result) <= 1000
    assert web._concise_result("An unfinished passage " * 80) == ""


def test_provisional_task_title_prefers_subject_and_removes_url():
    raw = SimpleNamespace(
        source_metadata={"subject": "Prepare for XY"},
        content="See https://example.org for details",
    )
    assert web.provisional_task_title(raw) == "Prepare for XY"
    raw.source_metadata = {}
    assert web.provisional_task_title(raw) == "See for details"


@pytest.mark.asyncio
async def test_web_research_updates_description_without_other_task_changes(monkeypatch):
    from app.agent.thread import runner

    task = SimpleNamespace(
        id=uuid.uuid4(), title="Prepare for XY", description="Prepare slides.",
        due_date=None, scheduled_date=None, estimation=None, location=None,
        link=None, label=None,
    )
    raw = SimpleNamespace(
        id=uuid.uuid4(), source="web_research", source_metadata={},
        content="XY starts at 10:00 in Room 4.", task_id=None, agent_trace=None,
    )
    calls = {}

    async def fake_chat(_messages, _settings, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            text="", stop_reason="tool_use", usage={}, provider="test", model="test",
            tool_calls=(ToolCall(id="update", name="update_task", input={
                "description": "Prepare slides. XY starts at 10:00 in Room 4."
            }),),
        )

    async def fake_apply(_session, _task, name, fields):
        calls["action"] = (name, fields)
        return {"outcome": "updated"}

    async def no_notes(*_args):
        return []

    monkeypatch.setattr(runner, "chat", fake_chat)
    monkeypatch.setattr(runner, "apply_task_action", fake_apply)
    monkeypatch.setattr(runner, "save_notes", no_notes)
    monkeypatch.setattr(runner.labels_store, "agent_descriptions", lambda *_: {})
    monkeypatch.setattr(runner.raw_inputs, "finalize", lambda *_a, **_k: None)

    await runner.run_thread_followup(SimpleNamespace(commit=lambda: None), raw, task)

    assert calls["force_tool"] == "update_task"
    assert [tool["name"] for tool in calls["tools"]] == ["update_task"]
    assert calls["action"] == ("update_task", {
        "description": "Prepare slides. XY starts at 10:00 in Room 4."
    })
