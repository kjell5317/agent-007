"""Thread follow-up flow: one LLM call to decide what to do with a reply.

When a raw input arrives on a thread we've already linked to a task, we skip
embedding + candidate search entirely and ask the LLM to pick one of:
`update_task`, `close_task`, `no_change`.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from sqlalchemy.orm import Session

from app.agent.prompts import THREAD_FOLLOWUP_SYSTEM_PROMPT
from app.agent.helpers.llm import (
    TERMINAL_TOOLS,
    block_summary,
    cached_system,
    cached_tools,
    create_message,
)
from app.agent.helpers.text import append_meta_lines, now_iso, parse_iso
from app.agent.tools import THREAD_FOLLOWUP_TOOLS
from app.config import get_settings
from app.db.clients import raw_inputs, tasks

log = logging.getLogger(__name__)


async def run_thread_followup(session: Session, raw, task) -> dict:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_msg = _build_thread_user_message(raw, task)
    trace: dict[str, Any] = {"outcome": None, "branch": "thread_followup", "task_id": str(task.id)}
    final_status = "open"
    final_task_id = task.id

    messages: list[MessageParam] = [{"role": "user", "content": user_msg}]
    log.info("llm call · branch=thread_followup raw=%s task=%s", raw.id, task.id)
    resp = await create_message(
        client, settings,
        system=cached_system(THREAD_FOLLOWUP_SYSTEM_PROMPT),
        tools=cached_tools(THREAD_FOLLOWUP_TOOLS),
        messages=messages,
    )
    log.debug(
        "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
        raw.id, resp.stop_reason,
        getattr(resp.usage, "input_tokens", "?"),
        getattr(resp.usage, "output_tokens", "?"),
    )
    trace["blocks"] = [block_summary(b) for b in resp.content]

    tool_uses = [
        b for b in resp.content
        if getattr(b, "type", None) == "tool_use" and b.name in TERMINAL_TOOLS
    ]
    if not tool_uses:
        trace["outcome"] = "no_tool_call"
    else:
        tu = tool_uses[0]
        tu_input = tu.input or {}
        if tu.name == "update_task":
            patch = {k: v for k, v in tu_input.items() if v is not None}
            if "due_date" in patch:
                patch["due_date"] = parse_iso(str(patch["due_date"]))
            tasks.update(session, task.id, **patch)
            trace["outcome"] = "updated"
            final_status = "open"
        elif tu.name == "close_task":
            trace["outcome"] = "closed"
            trace["confidence"] = tu_input.get("confidence")
            final_status = "closed"
        elif tu.name == "no_change":
            trace["outcome"] = "no_change"
            trace["confidence"] = tu_input.get("confidence")
            final_status = "open"
        else:
            trace["outcome"] = f"unknown_tool:{tu.name}"

    raw_inputs.finalize(
        session, raw.id, status=final_status, task_id=final_task_id, agent_trace=trace
    )
    session.commit()
    return trace


def _build_thread_user_message(raw, task) -> str:
    meta = raw.source_metadata or {}
    lines = [
        f"Current time: {now_iso(get_settings().user_timezone)}",
        f"Source: {raw.source}",
    ]
    append_meta_lines(lines, meta)

    lines.append("")
    lines.append("Current task:")
    lines.append(f"  id: {task.id}")
    lines.append(f"  title: {task.title}")
    if task.description:
        lines.append(f"  description: {task.description}")
    if task.due_date:
        lines.append(f"  due_date: {task.due_date.isoformat()}")
    if task.estimation is not None:
        lines.append(f"  estimation: {task.estimation} min")
    if task.location:
        lines.append(f"  location: {task.location}")
    if task.link:
        lines.append(f"  link: {task.link}")

    lines.append("")
    lines.append("Follow-up body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)
