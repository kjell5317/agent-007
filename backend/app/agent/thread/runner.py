"""Thread follow-up flow: one LLM call to decide what to do with a reply.

When a raw input arrives on a thread we've already linked to a task, we skip
embedding + candidate search entirely and ask the LLM to pick one of:
`update_task` (edit fields and/or change `status`) or `no_change`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agent.prompts import THREAD_FOLLOWUP_SYSTEM_PROMPT
from app.agent.helpers.llm import (
    LLMMessage,
    TERMINAL_TOOLS,
    block_summary,
    chat,
    user_message,
)
from app.agent.helpers.dispatch import apply_task_action
from app.agent.helpers.text import append_meta_lines, now_iso, task_field_lines
from app.agent.tools.notes_lookup import save_notes
from app.agent.tools import THREAD_FOLLOWUP_TOOLS
from app.config import get_settings
from app.db.clients import raw_inputs

log = logging.getLogger(__name__)


async def run_thread_followup(session: Session, raw, task) -> dict:
    settings = get_settings()

    user_msg = _build_thread_user_message(raw, task)
    trace: dict[str, Any] = {
        "outcome": None,
        "branch": "thread_followup",
        "task_id": str(task.id),
        "current_task": _task_trace_snapshot(task),
    }

    messages: list[LLMMessage] = [user_message(user_msg)]
    log.info("llm call · branch=thread_followup raw=%s task=%s", raw.id, task.id)
    resp = await chat(
        messages,
        settings,
        system_prompt=THREAD_FOLLOWUP_SYSTEM_PROMPT,
        tools=THREAD_FOLLOWUP_TOOLS,
    )
    log.debug(
        "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
        raw.id, resp.stop_reason,
        resp.usage.get("input_tokens", "?"),
        resp.usage.get("output_tokens", "?"),
    )
    trace["blocks"] = block_summary(resp)
    trace["llm"] = {
        "provider": resp.provider,
        "model": resp.model,
        "usage": resp.usage,
    }

    tool_uses = [
        b for b in resp.tool_calls if b.name in TERMINAL_TOOLS
    ]
    if not tool_uses:
        trace["outcome"] = "no_tool_call"
    else:
        tu = tool_uses[0]
        frag = await apply_task_action(session, task, tu.name, tu.input or {})
        trace.update(frag)
        saved = await save_notes(session, raw.id, (tu.input or {}).get("notes"))
        if saved:
            trace["notes_saved"] = saved
        trace["tool_results"] = [
            {
                "name": tu.name,
                "status": "success",
                "purpose": _tool_purpose(tu.name),
                "preview": str(frag.get("outcome") or "handled follow-up"),
                "result_summary": str(frag.get("outcome") or "handled follow-up"),
                "changed_state": frag.get("outcome") != "no_change",
                "artifact_refs": [f"task:{task.id}"],
            }
        ]

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


def _tool_purpose(name: str) -> str:
    if name == "update_task":
        return "update existing task"
    if name == "no_change":
        return "leave existing task unchanged"
    return name


def _task_trace_snapshot(task) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "id": str(task.id),
        "title": task.title,
    }
    if task.description:
        snapshot["description"] = task.description
    if task.due_date:
        snapshot["due_date"] = task.due_date.isoformat()
    if getattr(task, "scheduled_date", None):
        snapshot["scheduled_date"] = task.scheduled_date.isoformat()
    if task.estimation is not None:
        snapshot["estimation"] = task.estimation
    if task.location:
        snapshot["location"] = task.location
    if task.link:
        snapshot["link"] = task.link
    if task.label:
        snapshot["label"] = task.label
    return snapshot
