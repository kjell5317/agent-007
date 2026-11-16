"""Trace entries for hand-off agents, in the shape the new-input agent writes
into a raw input's `agent_trace`, so the frontend renders both alike."""

from __future__ import annotations

from typing import Any

from app.agent.helpers.llm import LLMResponse, block_summary

# Keeps a run's trace small enough to ship with every task read.
_RESULT_CHARS = 4000


def iteration(resp: LLMResponse) -> dict[str, Any]:
    return {
        "blocks": block_summary(resp),
        "llm": {"provider": resp.provider, "model": resp.model, "usage": resp.usage},
    }


def tool_entry(name: str, params: dict[str, Any], out: str) -> dict[str, Any]:
    failed = out.startswith(("Web search failed:", "Read failed:", f"{name} failed:", "unknown tool:"))
    detail = str(params.get("query") or params.get("url") or params.get("id") or "").strip()
    flat = " ".join(out.split())
    return {
        "name": name,
        "status": "failed" if failed else "success",
        "purpose": f"{name} {detail}".strip()[:120],
        "result_markdown": out[:_RESULT_CHARS],
        "preview": flat[:200],
        "result_summary": flat[:500],
        "changed_state": False,
        "artifact_refs": [],
    }
