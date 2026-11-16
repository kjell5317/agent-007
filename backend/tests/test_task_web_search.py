from types import SimpleNamespace

import pytest

from app.agent.helpers import web


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
        SimpleNamespace(gemini_api_key="test", chat_llm_model="gemini-3.8-flash"),
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
        "public fact", SimpleNamespace(gemini_api_key="test", chat_llm_model="gemini-3.8-flash")
    )
    assert result.startswith("Web search failed: no grounded search results")


def test_concise_result_keeps_complete_lines_only():
    lines = [f"- Fact {i}: " + "x" * 180 for i in range(8)]
    result = web._concise_result("\n".join(lines))
    assert result.splitlines() == lines[:5]
    assert len(result) <= 1000
    assert web._concise_result("An unfinished passage " * 80) == ""
