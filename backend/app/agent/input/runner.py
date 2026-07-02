"""New-input agent: decide create / duplicate / not_task for a fresh raw input.

Reached when the thread shortcut didn't apply and similarity-based auto
precedents didn't fire (see `orchestrator.process_raw_input`). The agent is
given the input plus the few most-similar past items, each tagged with its
status: OPEN / CLOSED tasks are actionable candidates (`update_task` can edit
fields and close/reopen them), while NOT_TASK items are precedent signals.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.agent.prompts import NEW_INPUT_SYSTEM_PROMPT
from app.agent.helpers.llm import (
    LLMMessage,
    MAX_TOOL_ITERATIONS,
    TERMINAL_TOOLS,
    assistant_message,
    block_summary,
    chat,
    tool_result_message,
    user_message,
)
from app.agent.helpers.dispatch import apply_task_action
from app.agent.tools.calendar_lookup import run_create_event, run_find_calendar_events
from app.agent.tools.notes_lookup import run_search_notes
from app.agent.helpers.text import (
    append_meta_lines,
    normalize_agent_due_date,
    now_iso,
    task_field_lines,
)
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
    candidates: list[SimilarInput],
    query_embedding: list[float] | None,
) -> dict:
    settings = get_settings()

    # Split the ranked candidates: OPEN/CLOSED tasks are actionable (load their
    # fields, dedup by task_id, keep similarity order), NOT_TASK items are
    # precedent signals.
    task_candidates: list[tuple[SimilarInput, Any]] = []
    seen_tasks: set[uuid.UUID] = set()
    for hit in candidates:
        if hit.task_id and hit.status in ("open", "closed") and hit.task_id not in seen_tasks:
            t = tasks.get(session, hit.task_id)
            if t is not None:
                seen_tasks.add(hit.task_id)
                task_candidates.append((hit, t))
    not_task_signals = [h for h in candidates if h.status == "not_task"]

    user_msg = _build_new_input_message(raw, task_candidates, not_task_signals)

    trace: dict[str, Any] = {
        "outcome": None,
        "branch": "new_input",
        "embedded_query": query_embedding is not None,
        "candidates": [_candidate_trace_ref(h) for h in candidates],
        "evidence_refs": [_candidate_trace_ref(h) for h in candidates],
        "iterations": [],
    }
    final_status = "not_task"
    final_task_id: uuid.UUID | None = None

    messages: list[LLMMessage] = [user_message(user_msg)]
    log.info(
        "llm call · branch=new_input raw=%s task_candidates=%d not_task_signals=%d",
        raw.id, len(task_candidates), len(not_task_signals),
    )

    done = False
    for _ in range(MAX_TOOL_ITERATIONS):
        resp = await chat(
            messages,
            settings,
            system_prompt=NEW_INPUT_SYSTEM_PROMPT,
            tools=NEW_INPUT_TOOLS,
        )
        log.debug(
            "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            raw.id, resp.stop_reason,
            resp.usage.get("input_tokens", "?"),
            resp.usage.get("output_tokens", "?"),
        )
        iter_log: dict[str, Any] = {
            "blocks": block_summary(resp),
            "llm": {
                "provider": resp.provider,
                "model": resp.model,
                "usage": resp.usage,
            },
        }
        trace["iterations"].append(iter_log)

        all_tool_uses = list(resp.tool_calls)
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
                    _tool_result_entry(
                        tu.name,
                        tin,
                        out,
                        status="failed" if tu.name not in {"search_notes", "find_calendar_events", "create_event"} else "success",
                        changed_state=tu.name == "create_event" and bool(trace.get("events_created")),
                        artifact_refs=[
                            f"event:{event_id}"
                            for event_id in trace.get("events_created", [])
                            if tu.name == "create_event"
                        ],
                    )
                )
                results.append(tool_result_message(tu, out))
            if not terminal_uses:
                messages.append(assistant_message(resp))
                messages.extend(results)
                continue

        if not terminal_uses:
            trace["outcome"] = trace["outcome"] or "no_tool_call"
            break

        tu = terminal_uses[0]
        tu_input = tu.input or {}

        if tu.name == "create_task":
            payload = dict(tu_input)
            due_date = normalize_agent_due_date(payload.get("due_date"))
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
                    due_date=due_date,
                    location=str(payload.get("location")) if payload.get("location") else None,
                    link=str(payload.get("link")) if payload.get("link") else None,
                    label=str(payload.get("label")) if payload.get("label") else None,
                ),
            )
            trace["outcome"] = "task_created"
            trace["task_id"] = str(task.id)
            iter_log.setdefault("tool_results", []).append(
                _tool_result_entry(
                    tu.name,
                    tu_input,
                    f"created task {task.id}",
                    changed_state=True,
                    artifact_refs=[f"task:{task.id}"],
                )
            )
            final_status = "open"
            final_task_id = task.id
            await schedule_task(session, task)
            done = True
            break
        if tu.name in ("no_change", "update_task"):
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
                iter_log.setdefault("tool_results", []).append(
                    _tool_result_entry(
                        tu.name,
                        tu_input,
                        "existing task was missing or invalid",
                        status="failed",
                        changed_state=False,
                    )
                )
                final_status = "duplicate"
                done = True
                break
            frag = await apply_task_action(session, existing_task, tu.name, tu_input)
            trace.update(frag)
            trace["existing_task_id"] = str(existing_id)
            selected_ref = _selected_candidate_ref(candidates, existing_id)
            if selected_ref:
                trace["selected_evidence_ref"] = selected_ref
            iter_log.setdefault("tool_results", []).append(
                _tool_result_entry(
                    tu.name,
                    tu_input,
                    str(frag.get("outcome") or "handled duplicate"),
                    changed_state=frag.get("outcome") != "no_change",
                    artifact_refs=[f"task:{existing_id}"],
                )
            )
            final_status = "duplicate"
            final_task_id = existing_id
            done = True
            break
        if tu.name == "mark_not_task":
            trace["outcome"] = "not_task"
            trace["reason"] = tu_input.get("reason")
            trace["confidence"] = tu_input.get("confidence")
            final_status = "not_task"
            raw_notes = tu_input.get("notes") or []
            saved = await _save_notes(session, raw.id, raw_notes)
            if saved:
                trace["notes_saved"] = saved
            iter_log.setdefault("tool_results", []).append(
                _tool_result_entry(
                    tu.name,
                    tu_input,
                    str(trace.get("reason") or "marked not task"),
                    changed_state=True,
                )
            )
            done = True
            break

        # Unknown tool — surface and stop.
        trace["outcome"] = f"unknown_tool:{tu.name}"
        iter_log.setdefault("tool_results", []).append(
            _tool_result_entry(
                tu.name,
                tu_input,
                "unknown terminal tool",
                status="failed",
                changed_state=False,
            )
        )
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
    raw,
    task_candidates: list[tuple[SimilarInput, Any]],
    not_task_signals: list[SimilarInput],
) -> str:
    meta = raw.source_metadata or {}
    lines = [
        f"Current time: {now_iso(get_settings().user_timezone)}",
        f"Source: {raw.source}",
    ]
    append_meta_lines(lines, meta, include_account=True)

    if task_candidates or not_task_signals:
        lines.append("")
        lines.append(
            "Most similar past items (ranked by similarity). If the input refers "
            "to an OPEN or CLOSED task below, act on it with `update_task` (pass "
            "its id as `existing_task_id`; set `status=closed` to finish it or "
            "`status=open` to reopen a CLOSED one) or `no_change`, instead of "
            "`create_task`. NOT_TASK items are precedents — a strong signal this "
            "input may also not be a task."
        )
        for hit, t in task_candidates:
            lines.extend(_task_candidate_lines(hit, t))
        for p in not_task_signals:
            lines.extend(_not_task_candidate_lines(p))

    lines.append("")
    lines.append("Body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)


def _task_candidate_lines(hit: SimilarInput, task: Any) -> list[str]:
    title = _truncate_inline(str(getattr(task, "title", "") or "(untitled task)"), 120)
    lines = [
        "",
        (
            f"[{hit.status.upper()}] sim={hit.similarity:.2f} · "
            f"existing_task_id={task.id} · title: {title}"
        ),
    ]
    for line in task_field_lines(task):
        if line.strip().lower().startswith("title:"):
            continue
        lines.append(line)
    return lines


def _not_task_candidate_lines(hit: SimilarInput) -> list[str]:
    title = _candidate_title(hit)
    lines = [
        "",
        f"[NOT_TASK] sim={hit.similarity:.2f} · title: {title}",
        f"  id: {hit.id}",
    ]

    meta = _candidate_metadata(hit)
    if meta:
        lines.append(f"  metadata: {meta}")

    snippet = _truncate_inline(hit.content_snippet or "", 300)
    if snippet:
        lines.append(f"  snippet: {snippet}")

    reason = _truncate_inline(str((hit.agent_trace or {}).get("reason") or ""), 180)
    if reason:
        lines.append(f"  reason: {reason}")

    return lines


def _candidate_title(hit: SimilarInput) -> str:
    subject = _truncate_inline(hit.subject or "", 120)
    if subject:
        return subject

    for raw_line in (hit.content_snippet or "").splitlines():
        line = _truncate_inline(raw_line, 120)
        if line:
            return line

    return "(no subject)"


def _candidate_trace_ref(hit: SimilarInput) -> dict[str, Any]:
    return {
        "ref": f"candidate:{hit.id}",
        "kind": "candidate",
        "id": str(hit.id),
        "status": hit.status,
        "source": hit.source,
        "task_id": str(hit.task_id) if hit.task_id else None,
        "similarity": round(hit.similarity, 4),
        "sim": round(hit.similarity, 4),
        "title": _candidate_title(hit),
        "snippet": _truncate_inline(hit.content_snippet or "", 300),
        "sender": hit.sender,
        "received_at": hit.received_at.isoformat() if hit.received_at else None,
    }


def _selected_candidate_ref(candidates: list[SimilarInput], task_id: uuid.UUID) -> str | None:
    for hit in candidates:
        if hit.task_id == task_id:
            return f"candidate:{hit.id}"
    return None


def _tool_result_entry(
    name: str,
    tool_input: dict[str, Any],
    summary: str,
    *,
    status: str = "success",
    changed_state: bool = False,
    artifact_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "purpose": _tool_purpose(name, tool_input),
        "preview": _truncate_inline(summary, 200),
        "result_summary": _truncate_inline(summary, 500),
        "changed_state": changed_state,
        "artifact_refs": artifact_refs or [],
    }


def _tool_purpose(name: str, tool_input: dict[str, Any]) -> str:
    if name == "search_notes":
        return f"search notes for {_truncate_inline(str(tool_input.get('query') or ''), 80)}"
    if name == "find_calendar_events":
        return "find calendar conflicts"
    if name == "create_event":
        return f"create calendar event {_truncate_inline(str(tool_input.get('summary') or ''), 80)}"
    if name == "create_task":
        return f"create task {_truncate_inline(str(tool_input.get('title') or ''), 80)}"
    if name == "update_task":
        return "update existing task"
    if name == "mark_not_task":
        return "mark input as not a task"
    if name == "no_change":
        return "leave existing task unchanged"
    return name


def _candidate_metadata(hit: SimilarInput) -> str:
    parts = [f"source={hit.source}"]
    sender = _truncate_inline(hit.sender or "", 100)
    if sender:
        parts.append(f"from={sender}")
    if hit.received_at:
        parts.append(f"received_at={hit.received_at.isoformat()}")
    return " · ".join(parts)


def _truncate_inline(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
