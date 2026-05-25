"""New-input agent: decide create / duplicate / not_task for a fresh raw input.

Reached when the thread shortcut didn't apply and similarity-based auto
precedents didn't fire (see `orchestrator.process_raw_input`). The agent is
given the input plus three candidate sets — open tasks (possible
duplicates), past not_task precedents, and closed-task precedents (signal
to consider a follow-up).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from sqlalchemy.orm import Session

from app.agent.prompts import NEW_INPUT_SYSTEM_PROMPT
from app.agent.runner.llm import (
    MAX_TOOL_ITERATIONS,
    TERMINAL_TOOLS,
    block_summary,
    cached_system,
    cached_tools,
    create_message,
)
from app.agent.runner.text import append_meta_lines, now_iso, parse_iso
from app.agent.tools import NEW_INPUT_TOOLS
from app.config import get_settings
from app.schemas.task import TaskCreate
from app.services.google_calendar import add_task_to_calendar
from app.services.notifications import notify_task_created
from app.storage import raw_inputs, tasks
from app.storage.raw_inputs import SimilarInput

log = logging.getLogger(__name__)


async def run_new_input_agent(
    session: Session,
    raw,
    open_hits: list[SimilarInput],
    closed_hits: list[SimilarInput],
    not_task_hits: list[SimilarInput],
    query_embedding: list[float] | None,
) -> dict:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Open-task candidates: deduplicate by task_id; load fields for the prompt.
    open_task_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for hit in open_hits:
        if hit.task_id and hit.task_id not in seen:
            seen.add(hit.task_id)
            open_task_ids.append(hit.task_id)

    open_tasks = [tasks.get(session, tid) for tid in open_task_ids]
    open_tasks = [t for t in open_tasks if t is not None]

    user_msg = _build_new_input_message(
        raw, open_tasks, not_task_hits, closed_hits
    )

    trace: dict[str, Any] = {
        "outcome": None,
        "branch": "new_input",
        "embedded_query": query_embedding is not None,
        "top_sim_open": round(open_hits[0].similarity, 4) if open_hits else None,
        "top_sim_not_task": round(not_task_hits[0].similarity, 4) if not_task_hits else None,
        "top_sim_closed": round(closed_hits[0].similarity, 4) if closed_hits else None,
        "iterations": [],
    }
    final_status = "not_task"
    final_task_id: uuid.UUID | None = None

    messages: list[MessageParam] = [{"role": "user", "content": user_msg}]
    log.info(
        "llm call · branch=new_input raw=%s candidates=%d not_task_precedents=%d closed_precedents=%d",
        raw.id, len(open_tasks), len(not_task_hits), len(closed_hits),
    )

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = await create_message(
            client, settings,
            system=cached_system(NEW_INPUT_SYSTEM_PROMPT),
            tools=cached_tools(NEW_INPUT_TOOLS),
            messages=messages,
        )
        log.debug(
            "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            raw.id, resp.stop_reason,
            getattr(resp.usage, "input_tokens", "?"),
            getattr(resp.usage, "output_tokens", "?"),
        )
        iter_log: dict[str, Any] = {
            "blocks": [block_summary(b) for b in resp.content],
        }
        trace["iterations"].append(iter_log)

        tool_uses = [
            b for b in resp.content
            if getattr(b, "type", None) == "tool_use" and b.name in TERMINAL_TOOLS
        ]
        if not tool_uses:
            trace["outcome"] = trace["outcome"] or "no_tool_call"
            break

        tu = tool_uses[0]
        tu_input = tu.input or {}

        if tu.name == "create_task":
            payload = dict(tu_input)
            if "due_date" in payload:
                payload["due_date"] = parse_iso(str(payload["due_date"]))
            task = tasks.create(
                session,
                TaskCreate(
                    title=str(payload["title"]),
                    description=str(payload.get("description")) if payload.get("description") else None,
                    estimation=payload.get("estimation") if payload.get("estimation") else None,
                    due_date=parse_iso(str(payload.get("due_date"))) if payload.get("due_date") else None,
                    location=str(payload.get("location")) if payload.get("location") else None,
                    link=str(payload.get("link")) if payload.get("link") else None,
                    label=str(payload.get("label")) if payload.get("label") else None,
                    ai_doable=str(payload.get("ai_doable")) if payload.get("ai_doable") else None,
                ),
            )
            trace["outcome"] = "task_created"
            trace["task_id"] = str(task.id)
            final_status = "open"
            final_task_id = task.id
            await notify_task_created(task, raw)
            await add_task_to_calendar(session, task)
            break
        if tu.name == "mark_duplicate":
            existing_id = uuid.UUID(tu_input["existing_task_id"])
            trace["outcome"] = "duplicate"
            trace["existing_task_id"] = str(existing_id)
            trace["confidence"] = tu_input.get("confidence")
            final_status = "duplicate"
            final_task_id = existing_id
            break
        if tu.name == "mark_not_task":
            trace["outcome"] = "not_task"
            trace["confidence"] = tu_input.get("confidence")
            final_status = "not_task"
            break

        # Unknown tool — surface and stop.
        trace["outcome"] = f"unknown_tool:{tu.name}"
        break
    else:
        trace["outcome"] = trace["outcome"] or "max_iterations"

    raw_inputs.finalize(
        session,
        raw.id,
        status=final_status,
        task_id=final_task_id,
        agent_trace=trace,
    )
    session.commit()
    return trace


def _build_new_input_message(
    raw, open_tasks, not_task_hits: list[SimilarInput], closed_hits: list[SimilarInput]
) -> str:
    meta = raw.source_metadata or {}
    lines = [f"Current time: {now_iso()}", f"Source: {raw.source}"]
    append_meta_lines(lines, meta, include_account=True)

    if open_tasks:
        lines.append("")
        lines.append("Candidate existing tasks (use `mark_duplicate` if one matches):")
        for t in open_tasks:
            desc = (t.description or "").strip().replace("\n", " ")
            if len(desc) > 160:
                desc = desc[:160] + "…"
            lines.append(f"- {t.id} | {t.title}" + (f" — {desc}" if desc else ""))

    precedent_lines: list[str] = []
    for p in not_task_hits[:3]:
        reason = (p.agent_trace or {}).get("reason") or ""
        precedent_lines.append(
            f"- sim={p.similarity:.2f} | NOT_TASK | from {p.sender or '?'} | "
            f"{p.subject or '(no subject)'} | {reason[:120]}"
        )
    for p in closed_hits[:3]:
        precedent_lines.append(
            f"- sim={p.similarity:.2f} | CLOSED_TASK_PRECEDENT (task {p.task_id}) | "
            f"from {p.sender or '?'} | {p.subject or '(no subject)'}"
        )
    if precedent_lines:
        lines.append("")
        lines.append("Past similar inputs (precedents — strong signal):")
        lines.extend(precedent_lines)

    lines.append("")
    lines.append("Body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)
