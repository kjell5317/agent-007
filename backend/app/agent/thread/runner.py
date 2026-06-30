"""Thread follow-up flow: one LLM call to decide what to do with a reply.

When a raw input arrives on a thread we've already linked to a task, we skip
embedding + candidate search entirely and ask the LLM to pick one of:
`update_task` (edit fields and/or change `status`) or `no_change`.
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
from app.agent.helpers.dispatch import apply_task_action
from app.agent.helpers.text import append_meta_lines, now_iso, task_field_lines
from app.agent.tools import THREAD_FOLLOWUP_TOOLS
from app.config import get_settings
from app.db.clients import raw_inputs

log = logging.getLogger(__name__)


async def run_thread_followup(session: Session, raw, task) -> dict:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_msg = _build_thread_user_message(raw, task)
    trace: dict[str, Any] = {"outcome": None, "branch": "thread_followup", "task_id": str(task.id)}

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
        frag = await apply_task_action(session, task, tu.name, tu.input or {})
        trace.update(frag)

    # The follow-up references an existing task; its lifecycle state lives on
    # that task's own anchor row, which close/reopen flip directly. Recording
    # the follow-up as a `duplicate` keeps it out of status derivation, so a
    # `no_change` (or a fields-only edit) never flips the task's state.
    raw_inputs.finalize(
        session, raw.id, status="duplicate", task_id=task.id, agent_trace=trace
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
    lines.extend(task_field_lines(task))

    lines.append("")
    lines.append("Follow-up body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)
