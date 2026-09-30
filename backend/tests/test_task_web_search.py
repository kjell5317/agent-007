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
    async def fake_chat(*_args, **_kwargs):
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
    )
    assert context == "Useful details"
    assert trace["search_queries"] == 2
    assert trace["search_queries_estimated"] is False
