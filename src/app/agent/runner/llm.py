"""Anthropic client wrapper shared by every agent flow in this package.

Centralizes the bits that would otherwise be duplicated across each flow:
the Messages API call (with MCP beta when configured), prompt/tool caching,
block-summary extraction for trace logs, and the shared tuning constants.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import ToolParam
from anthropic.types.beta import BetaRequestMCPServerURLDefinitionParam

from app.config import Settings

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 3
MAX_TOKENS = 1024
TEMPERATURE = 0.4
MCP_BETA = "mcp-client-2025-04-04"

TERMINAL_TOOLS = frozenset({
    "create_task", "mark_duplicate", "mark_not_task",
    "update_task", "close_task", "no_change",
})


def cached_system(prompt: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def cached_tools(tools: list[dict]) -> list[ToolParam]:
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return cast(list[ToolParam], out)


async def create_message(client: AsyncAnthropic, settings: Settings, **kwargs: Any):
    """Issue a Messages API call, routing through the beta endpoint when MCP is on.

    When at least one MCP server is configured, the agent gains access to the
    server's tools mid-decision (server-side execution by Anthropic); when
    none are configured, behavior is identical to the prior direct call.
    """
    base = dict(
        model=settings.claude_model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
    )
    servers = _mcp_servers(settings)
    if servers:
        return await client.beta.messages.create(
            **base,
            mcp_servers=cast(list[BetaRequestMCPServerURLDefinitionParam], servers),
            betas=[MCP_BETA],
            **kwargs,
        )
    return await client.messages.create(**base, **kwargs)


def block_summary(block) -> dict:
    btype = getattr(block, "type", "unknown")
    if btype == "text":
        return {"type": "text", "text": getattr(block, "text", "") or ""}
    if btype == "tool_use":
        return {"type": "tool_use", "name": block.name, "input": block.input}
    if btype == "mcp_tool_use":
        return {
            "type": "mcp_tool_use",
            "server": getattr(block, "server_name", None),
            "name": getattr(block, "name", None),
        }
    if btype == "mcp_tool_result":
        return {
            "type": "mcp_tool_result",
            "tool_use_id": getattr(block, "tool_use_id", None),
            "is_error": bool(getattr(block, "is_error", False)),
        }
    return {"type": btype}


def _mcp_servers(settings: Settings) -> list[dict[str, Any]]:
    """Build the MCP server list, silently skipping any pair that isn't both
    URL and token set. Empty list → caller stays on the non-beta endpoint."""
    candidates = [
        ("github", settings.github_mcp_url, settings.github_mcp_token),
        ("notion", settings.notion_mcp_url, settings.notion_mcp_token),
    ]
    servers: list[dict[str, Any]] = []
    skipped: list[str] = []
    for name, url, token in candidates:
        entry = _mcp_entry(name, url, token)
        if entry is None:
            skipped.append(name)
        else:
            servers.append(entry)
    if skipped:
        log.debug("mcp · skipped (missing url or token): %s", ", ".join(skipped))
    return servers


def _mcp_entry(name: str, url: str, token: str) -> dict[str, Any] | None:
    url = (url or "").strip()
    token = (token or "").strip()
    if not url or not token:
        return None
    return {
        "name": name,
        "type": "url",
        "url": url,
        "authorization_token": token,
    }
