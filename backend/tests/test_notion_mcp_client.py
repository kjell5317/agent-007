"""Notion MCP client service, network-free: fake the MCP session and token so we
exercise tool-name resolution, argument shaping, and result extraction."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.services import notion_mcp


@dataclass
class _FakeToken:
    access_token: str = "tok"
    extra: dict = field(default_factory=lambda: {"mcp_server_url": "https://mcp.notion.com/mcp"})


class _CM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


@dataclass
class _Tool:
    name: str


@dataclass
class _ListResult:
    tools: list


@dataclass
class _Content:
    text: str
    type: str = "text"


@dataclass
class _CallResult:
    content: list
    isError: bool = False
    structuredContent: dict | None = None


class _FakeSession:
    def __init__(self, tool_names, result):
        self._tool_names = tool_names
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self):
        return None

    async def list_tools(self):
        return _ListResult([_Tool(n) for n in self._tool_names])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self._result


def _install(monkeypatch, *, tool_names, result):
    notion_mcp._resolved.clear()
    session = _FakeSession(tool_names, result)

    async def fake_token(_db):
        return _FakeToken()

    monkeypatch.setattr(notion_mcp, "get_fresh_notion_token", fake_token)
    monkeypatch.setattr(
        notion_mcp, "streamablehttp_client", lambda url, headers, timeout: _CM((None, None, None))
    )
    monkeypatch.setattr(notion_mcp, "ClientSession", lambda read, write: _CM(session))
    return session


@pytest.mark.asyncio
async def test_search_resolves_prefixed_name_and_passes_query(monkeypatch):
    session = _install(
        monkeypatch,
        tool_names=["notion-search", "notion-fetch", "notion-create-pages"],
        result=_CallResult([_Content("Roadmap — https://notion.so/roadmap")]),
    )
    out = await notion_mcp.notion_search(object(), "roadmap")
    assert "Roadmap" in out
    assert session.calls == [("notion-search", {"query": "roadmap"})]


@pytest.mark.asyncio
async def test_fetch_falls_back_to_bare_name_and_passes_id(monkeypatch):
    session = _install(
        monkeypatch,
        tool_names=["search", "fetch"],  # older, unprefixed naming
        result=_CallResult([_Content("page body")]),
    )
    out = await notion_mcp.notion_fetch(object(), "abc-123")
    assert out == "page body"
    assert session.calls == [("fetch", {"id": "abc-123"})]


@pytest.mark.asyncio
async def test_error_result_raises(monkeypatch):
    _install(
        monkeypatch,
        tool_names=["notion-search"],
        result=_CallResult([_Content("nope")], isError=True),
    )
    with pytest.raises(RuntimeError, match="Notion MCP tool error"):
        await notion_mcp.notion_search(object(), "x")


@pytest.mark.asyncio
async def test_missing_tool_raises(monkeypatch):
    _install(
        monkeypatch,
        tool_names=["notion-create-pages"],  # no search/fetch at all
        result=_CallResult([_Content("")]),
    )
    with pytest.raises(RuntimeError, match="no search tool"):
        await notion_mcp.notion_search(object(), "x")


@pytest.mark.asyncio
async def test_structured_content_fallback_when_no_text(monkeypatch):
    _install(
        monkeypatch,
        tool_names=["notion-fetch"],
        result=_CallResult([], structuredContent={"id": "p1"}),
    )
    out = await notion_mcp.notion_fetch(object(), "p1")
    assert "p1" in out
