"""Agent loop: turn a single RawInput into a decision (create / duplicate / skip).

Kept intentionally thin and source-agnostic. The runner:

  1. Loads the raw input + a small set of dedup candidates.
  2. Calls Claude once with the system prompt + tool schemas (cached).
  3. Dispatches the (terminal) tool call to storage.
  4. Writes the agent trace back onto the RawInput for audit/replay.

Per-input cost is one LLM round trip: candidates are pre-fetched into the
user message and every tool short-circuits the loop.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlock, ToolParam, ToolUseBlock
from sqlalchemy.orm import Session

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.config import get_settings
from app.models.task import Task
from app.schemas.feedback import FeedbackCreate
from app.schemas.task import TaskCreate
from app.storage import feedback, raw_inputs, tasks

MAX_TOOL_ITERATIONS = 4
MAX_TOKENS = 1024
TEMPERATURE = 0.4
DEDUP_CANDIDATES = 10

TERMINAL_TOOLS = frozenset({"create_task", "mark_duplicate", "mark_not_a_task"})


async def process_raw_input(session: Session, raw_input_id: uuid.UUID) -> dict:
    """Run the agent over one raw input and persist the outcome.

    Idempotent on raw_input_id — re-running a processed input is a no-op.
    Returns a small summary dict (mirrored into RawInput.agent_trace).
    """
    raw = raw_inputs.get(session, raw_input_id)
    if raw is None:
        return {"outcome": "missing"}
    if raw.processed_at is not None:
        return {"outcome": "already_processed", "status": raw.status}

    candidates = tasks.list_(session, status="open", limit=DEDUP_CANDIDATES)
    user_content = _build_user_message(
        raw.source, raw.content, raw.source_metadata or {}, candidates
    )

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    messages: list[MessageParam] = [{"role": "user", "content": user_content}]
    trace: dict[str, Any] = {"iterations": [], "outcome": None, "candidates": len(candidates)}
    final_status = "processed"

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = await client.messages.create(
            model=settings.claude_model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=_cached_system(),
            tools=_cached_tools(),
            messages=messages,
        )

        iter_log: dict[str, Any] = {
            "stop_reason": resp.stop_reason,
            "blocks": [_block_summary(b) for b in resp.content],
        }
        trace["iterations"].append(iter_log)

        tool_uses = [b for b in resp.content if isinstance(b, ToolUseBlock)]
        if not tool_uses:
            trace["outcome"] = trace["outcome"] or "no_tool_call"
            break

        terminal_hit = False
        for tu in tool_uses:
            result = _dispatch_tool(session, raw_input_id, tu.name, tu.input or {})
            iter_log.setdefault("tool_calls", []).append(
                {"name": tu.name, "input": tu.input, "result": result}
            )

            if tu.name == "create_task":
                trace["outcome"] = "task_created"
                trace["task_id"] = result.get("task_id")
                terminal_hit = True
            elif tu.name == "mark_duplicate":
                trace["outcome"] = "duplicate"
                trace["existing_task_id"] = result.get("existing_task_id")
                final_status = "skipped"
                terminal_hit = True
            elif tu.name == "mark_not_a_task":
                trace["outcome"] = "not_a_task"
                trace["reason"] = result.get("reason")
                final_status = "skipped"
                terminal_hit = True

        # Every tool is terminal — short-circuit before another model call.
        if terminal_hit:
            break

        # Defensive: only non-terminal tools (none today) would feed results back.
        messages.append(
            cast(
                MessageParam,
                {"role": "assistant", "content": [_block_to_dict(b) for b in resp.content]},
            )
        )
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(
                    iter_log["tool_calls"][i]["result"], default=str
                ),
            }
            for i, tu in enumerate(tool_uses)
        ]
        messages.append(cast(MessageParam, {"role": "user", "content": tool_results}))
    else:
        trace["outcome"] = trace["outcome"] or "max_iterations"

    raw_inputs.mark_processed(
        session, raw_input_id, status=final_status, agent_trace=trace
    )
    session.commit()
    return trace


def _cached_system() -> list[dict[str, Any]]:
    """System prompt as a single cache-controlled block.

    Anthropic prompt caching: every request with the same prefix bytes (system
    + tools) hits the cache for 5 minutes after the first call, cutting cost
    on the static portion to ~10%.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _cached_tools() -> list[ToolParam]:
    """Tools list with cache_control on the final entry.

    Marking the last tool caches every preceding tool definition as well.
    """
    out = [dict(t) for t in TOOLS]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return cast(list[ToolParam], out)


def _build_user_message(
    source: str, content: str, metadata: dict, candidates: list[Task]
) -> str:
    lines = [f"Source: {source}"]
    for key in ("from", "to", "subject", "date", "thread_id", "account"):
        val = metadata.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")
    urls = metadata.get("urls") or []
    if urls:
        lines.append(f"URLs: {', '.join(urls)}")

    if candidates:
        lines.append("")
        lines.append("Candidate existing tasks (use `mark_duplicate` if one matches):")
        for c in candidates:
            desc = (c.description or "").strip().replace("\n", " ")
            if len(desc) > 160:
                desc = desc[:160] + "…"
            lines.append(f"- {c.id} | {c.title}" + (f" — {desc}" if desc else ""))

    lines.append("")
    lines.append("Body:")
    lines.append(content.strip() or "(empty)")
    return "\n".join(lines)


def _dispatch_tool(
    session: Session, raw_input_id: uuid.UUID, name: str, payload: dict
) -> dict:
    if name == "create_task":
        return _tool_create_task(session, raw_input_id, payload)
    if name == "mark_duplicate":
        return _tool_mark_duplicate(session, raw_input_id, payload)
    if name == "mark_not_a_task":
        return _tool_mark_not_a_task(session, raw_input_id, payload)
    return {"error": f"unknown tool {name!r}"}


def _tool_create_task(session: Session, raw_input_id: uuid.UUID, payload: dict) -> dict:
    create = TaskCreate(
        title=payload["title"],
        description=payload.get("description"),
        estimated_minutes=payload.get("estimated_minutes"),
        location=payload.get("location"),
        due_at=payload.get("due_at"),
        source_links=payload.get("source_links") or [],
        confidence=payload.get("confidence"),
        raw_input_id=raw_input_id,
    )
    row = tasks.create(session, create)
    return {"task_id": str(row.id), "title": row.title}


def _tool_mark_duplicate(session: Session, raw_input_id: uuid.UUID, payload: dict) -> dict:
    existing_id = uuid.UUID(payload["existing_task_id"])
    reason = payload.get("reason")
    feedback.create(
        session,
        FeedbackCreate(
            task_id=existing_id,
            raw_input_id=raw_input_id,
            kind="duplicate_of",
            note=reason,
        ),
    )
    return {"existing_task_id": str(existing_id), "reason": reason}


def _tool_mark_not_a_task(session: Session, raw_input_id: uuid.UUID, payload: dict) -> dict:
    reason = payload.get("reason", "")
    feedback.create(
        session,
        FeedbackCreate(
            raw_input_id=raw_input_id,
            kind="not_a_task",
            note=reason,
        ),
    )
    return {"reason": reason}


def _block_summary(block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": (block.text or "")[:500]}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": block.input}
    return {"type": getattr(block, "type", "unknown")}


def _block_to_dict(block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": getattr(block, "type", "unknown")}
