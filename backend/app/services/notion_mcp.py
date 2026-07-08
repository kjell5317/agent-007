"""Read-only Notion access for the chat agent via Notion's hosted MCP server.

The chat agent gets two thin tools — `notion_search` and `notion_fetch` — that
proxy to the corresponding read-only tools on Notion's hosted MCP server
(streamable HTTP, OAuth bearer). Only these two are wired, so the agent can read
the workspace but never mutate it.

Each call opens a short-lived MCP session with a freshly resolved (refreshed if
needed) access token. Server-side tool names are discovered once per process and
cached, since Notion has shipped both `search`/`fetch` and `notion-search`/
`notion-fetch` naming.
"""

from __future__ import annotations

import logging

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult
from sqlalchemy.orm import Session

from app.auth.notion_tokens import get_fresh_notion_token, is_connected
from app.config import get_settings

log = logging.getLogger(__name__)

__all__ = ["is_connected", "notion_search", "notion_fetch"]

_TIMEOUT = 30

# want -> ordered server-side tool-name candidates.
_TOOL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "search": ("notion-search", "search"),
    "fetch": ("notion-fetch", "fetch"),
}
# Resolved {server_url: {want: actual_name}}, cached for the process.
_resolved: dict[str, dict[str, str]] = {}


async def notion_search(session: Session, query: str) -> str:
    return await _call(session, "search", {"query": query})


async def notion_fetch(session: Session, ref: str) -> str:
    return await _call(session, "fetch", {"id": ref})


async def _call(session: Session, want: str, arguments: dict) -> str:
    token = await get_fresh_notion_token(session)
    url = str(token.extra.get("mcp_server_url") or get_settings().mcp_notion_url)
    headers = {"Authorization": f"Bearer {token.access_token}"}
    async with streamablehttp_client(url, headers=headers, timeout=_TIMEOUT) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            name = await _resolve(mcp, url, want)
            result = await mcp.call_tool(name, arguments)
    return _result_text(result)


async def _resolve(mcp: ClientSession, url: str, want: str) -> str:
    cached = _resolved.get(url, {})
    if want in cached:
        return cached[want]
    names = {tool.name for tool in (await mcp.list_tools()).tools}
    for candidate in _TOOL_CANDIDATES[want]:
        if candidate in names:
            _resolved.setdefault(url, {})[want] = candidate
            return candidate
    raise RuntimeError(
        f"Notion MCP server exposes no {want} tool "
        f"(tried {_TOOL_CANDIDATES[want]}; available: {sorted(names)})"
    )


def _result_text(result: CallToolResult) -> str:
    parts = [
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text" and block.text
    ]
    text = "\n".join(parts).strip()
    if not text and result.structuredContent:
        text = str(result.structuredContent)
    if result.isError:
        raise RuntimeError(f"Notion MCP tool error: {text or 'unknown error'}")
    return text or "(no content)"
