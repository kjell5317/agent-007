from types import SimpleNamespace

import pytest

from app.agent.helpers import web


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
