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
from app.agent.helpers.llm import (
    MAX_TOOL_ITERATIONS,
    TERMINAL_TOOLS,
    block_summary,
    cached_system,
    cached_tools,
    create_message,
)
from app.agent.helpers.dispatch import apply_task_action
from app.agent.tools.calendar_lookup import run_create_event, run_find_calendar_events
from app.agent.tools.notes_lookup import run_search_notes
from app.agent.helpers.text import append_meta_lines, now_iso, parse_iso, task_field_lines
from app.agent.tools import NEW_INPUT_TOOLS
from app.config import get_settings
from app.services.input.embedding import embed
from app.db.schemas.task import TaskCreate
from app.services.plan import schedule_task
from app.db.clients import notes as notes_store, raw_inputs, tasks
from app.db.clients.raw_inputs import SimilarInput

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

    done = False
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

        all_tool_uses = [
            b for b in resp.content if getattr(b, "type", None) == "tool_use"
        ]
        terminal_uses = [b for b in all_tool_uses if b.name in TERMINAL_TOOLS]
        non_terminal_uses = [b for b in all_tool_uses if b.name not in TERMINAL_TOOLS]

        # Run non-terminal tools (lookups + event creation). Their results feed
        # the next decision, so we run them whether or not a terminal tool rode
        # along in the same response — a `create_event` emitted next to
        # `mark_not_task` must still take effect. When no terminal tool is
        # present we feed the results back and loop; when one is present we fall
        # through to handle it (the run ends, so no further API call is made and
        # the tool_results don't need to be appended).
        if non_terminal_uses:
            results = []
            for tu in non_terminal_uses:
                tin = tu.input or {}
                if tu.name == "search_notes":
                    out = await run_search_notes(session, str(tin.get("query") or ""))
                elif tu.name == "find_calendar_events":
                    out = await run_find_calendar_events(
                        session,
                        str(tin.get("time_min") or ""),
                        str(tin.get("time_max") or ""),
                    )
                elif tu.name == "create_event":
                    out, event_id = await run_create_event(
                        session,
                        summary=str(tin.get("summary") or ""),
                        start=str(tin.get("start") or ""),
                        end=str(tin.get("end")) if tin.get("end") else None,
                        description=str(tin.get("description")) if tin.get("description") else None,
                        location=str(tin.get("location")) if tin.get("location") else None,
                    )
                    if event_id:
                        trace.setdefault("events_created", []).append(event_id)
                else:
                    out = f"unknown tool: {tu.name}"
                iter_log.setdefault("tool_results", []).append(
                    {"name": tu.name, "preview": out[:200]}
                )
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": out,
                })
            if not terminal_uses:
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": results})
                continue

        if not terminal_uses:
            trace["outcome"] = trace["outcome"] or "no_tool_call"
            break

        tu = terminal_uses[0]
        tu_input = tu.input or {}

        if tu.name == "create_task":
            payload = dict(tu_input)
            if "due_date" in payload:
                payload["due_date"] = parse_iso(str(payload["due_date"]))
            # The schema marks `label` required, but the LLM sometimes skips
            # it — warn and leave NULL so the user can assign one later.
            if not payload.get("label"):
                log.warning(
                    "agent skipped label · raw=%s — leaving NULL, user must assign",
                    raw.id,
                )
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
                ),
            )
            trace["outcome"] = "task_created"
            trace["task_id"] = str(task.id)
            final_status = "open"
            final_task_id = task.id
            await schedule_task(session, task)
            done = True
            break
        if tu.name in ("no_change", "update_task", "close_task"):
            # Duplicate-handling: act on the named candidate task. The current
            # input stays status="duplicate" linked to that task regardless of
            # the action — the action mutates the task's anchor row, not this one.
            existing_raw = tu_input.get("existing_task_id")
            existing_id = uuid.UUID(str(existing_raw)) if existing_raw else None
            existing_task = tasks.get(session, existing_id) if existing_id else None
            if existing_task is None:
                log.warning(
                    "duplicate action %s · raw=%s missing/invalid existing_task_id=%s",
                    tu.name, raw.id, existing_raw,
                )
                trace["outcome"] = "duplicate_target_missing"
                final_status = "duplicate"
                done = True
                break
            frag = await apply_task_action(session, existing_task, tu.name, tu_input)
            trace.update(frag)
            trace["existing_task_id"] = str(existing_id)
            final_status = "duplicate"
            final_task_id = existing_id
            done = True
            break
        if tu.name == "mark_not_task":
            trace["outcome"] = "not_task"
            trace["confidence"] = tu_input.get("confidence")
            final_status = "not_task"
            raw_notes = tu_input.get("notes") or []
            saved = await _save_notes(session, raw.id, raw_notes)
            if saved:
                trace["notes_saved"] = saved
            done = True
            break

        # Unknown tool — surface and stop.
        trace["outcome"] = f"unknown_tool:{tu.name}"
        done = True
        break
    if not done:
        trace["outcome"] = trace["outcome"] or "max_iterations"

    # An input that only produced a calendar event (attending needs no action)
    # finalizes as "event" rather than "not_task", which would misrepresent it.
    # When a task was created or a duplicate handled, that record takes priority.
    if trace.get("events_created") and final_status == "not_task":
        final_status = "event"

    raw_inputs.finalize(
        session,
        raw.id,
        status=final_status,
        task_id=final_task_id,
        agent_trace=trace,
    )
    session.commit()
    return trace


async def _save_notes(session, raw_input_id, raw_notes) -> list[str]:
    """Persist the notes the agent attached to `mark_not_task`. Each note is
    embedded so future `search_notes` calls can retrieve it. Returns the
    list of saved note contents (for the trace)."""
    saved: list[str] = []
    if not isinstance(raw_notes, list):
        return saved
    for entry in raw_notes:
        content = str(entry or "").strip()
        if not content:
            continue
        vec = await embed(content)
        notes_store.create(
            session,
            content=content,
            source_raw_input_id=raw_input_id,
            embedding=vec,
        )
        saved.append(content)
    if saved:
        session.commit()
    return saved


def _build_new_input_message(
    raw, open_tasks, not_task_hits: list[SimilarInput], closed_hits: list[SimilarInput]
) -> str:
    meta = raw.source_metadata or {}
    lines = [
        f"Current time: {now_iso(get_settings().user_timezone)}",
        f"Source: {raw.source}",
    ]
    append_meta_lines(lines, meta, include_account=True)

    if open_tasks:
        lines.append("")
        lines.append(
            "Candidate existing tasks — if the input duplicates one of these, "
            "act on it with `no_change` / `update_task` / `close_task` and pass "
            "its id as `existing_task_id` instead of calling `create_task`:"
        )
        for t in open_tasks:
            lines.append("")
            lines.extend(task_field_lines(t))

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
