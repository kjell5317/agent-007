"""Field-extraction agent for manually-promoted inputs.

Used for fresh manual entries and promoted inputs. The user has already
decided the input is task-related; the agent creates a task or acts on a
matching existing task.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agent.prompts import EXTRACT_FIELDS_SYSTEM_PROMPT
from app.agent.helpers.web import first_input_url, provisional_task_title, research_link
from app.agent.helpers.llm import (
    LLMMessage,
    MAX_TOOL_ITERATIONS,
    assistant_message,
    block_summary,
    chat,
    tool_result_message,
    user_message,
)
from app.agent.tools.notes_lookup import run_search_notes, save_notes
from app.agent.tools.calendar_lookup import (
    run_find_calendar_events, run_get_event_details, run_update_event,
)
from app.agent.helpers.text import normalize_agent_due_date, now_iso
from app.agent.helpers.precedents import (
    candidate_trace_ref,
    task_candidate_lines,
)
from app.agent.tools import INPUT_LINK_RESEARCH_TOOL, new_input_tools
from app.config import get_settings
from app.db.clients import labels as labels_store
from app.db.clients import tasks
from app.db.clients.raw_inputs import SimilarInput
from app.services.event_context import apply_event_context, mentions_event

log = logging.getLogger(__name__)


class ExtractionFailed(RuntimeError):
    def __init__(self, message: str, trace: dict[str, Any]):
        super().__init__(message)
        self.trace = trace


async def extract_task_fields(
    session: Session,
    raw,
    *,
    context_inputs=(),
    precedent_candidates: list[SimilarInput] | None = None,
    include_trace: bool = False,
    harvest_notes: bool = True,
    research: bool = True,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
    """Ask the LLM to extract task fields from a raw input.

    `context_inputs` are sibling raw_inputs from the same thread/follow-up
    group. When present, the agent sees the whole conversation (oldest first)
    and is told to produce ONE task capturing it.

    `harvest_notes=False` drops the `notes` field so the agent can't write to
    long-term memory — used for kotx briefs, whose coding details would only
    spam the notes store.

    Multi-step loop so the model can call `search_notes` before choosing a
    terminal task action. Without task candidates, the last iteration forces
    `create_task` so extraction ends with a populated payload."""
    settings = get_settings()

    precedent_candidates = [
        hit for hit in (precedent_candidates or [])
        if hit.task_id and hit.status in ("open", "closed")
    ]
    candidate_task_ids = {str(hit.task_id) for hit in precedent_candidates}
    user_msg = _build_extract_message(session, raw, context_inputs, precedent_candidates)
    source_url = first_input_url(raw) if raw.source == "manual" else None
    tools = new_input_tools(labels_store.agent_descriptions(session))
    create_tool = next(t for t in tools if t["name"] == "create_task")
    if not harvest_notes:
        create_tool = _without_notes(create_tool)
    search_tool = next(t for t in tools if t["name"] == "search_notes")
    calendar_tool = next(t for t in tools if t["name"] == "find_calendar_events")
    event_details_tool = next(t for t in tools if t["name"] == "get_event_details")
    event_update_tool = next(t for t in tools if t["name"] == "update_event")
    extract_tools = [
        search_tool, calendar_tool, event_details_tool, event_update_tool, create_tool
    ]
    research_enabled = bool(
        source_url and research and getattr(settings, "task_web_search", False)
        and getattr(settings, "gemini_api_key", "")
    )
    if research_enabled:
        extract_tools.insert(0, INPUT_LINK_RESEARCH_TOOL)
    researched_link = False
    if candidate_task_ids:
        extract_tools.append(next(tool for tool in tools if tool["name"] == "update_task"))

    messages: list[LLMMessage] = [user_message(user_msg)]
    log.info("llm call · branch=extract_fields raw=%s", raw.id)

    payload: dict[str, Any] = {}
    trace: dict[str, Any] = {
        "branch": "manual",
        "candidates": [candidate_trace_ref(h) for h in precedent_candidates],
        "evidence_refs": [candidate_trace_ref(h) for h in precedent_candidates],
        "iterations": [],
    }
    iteration_limit = MAX_TOOL_ITERATIONS + 2 if mentions_event(raw.content or "") else MAX_TOOL_ITERATIONS
    for attempt in range(iteration_limit - 1):
        is_last = attempt == iteration_limit - 2
        # On the last turn, require a terminal action. Candidate matches need
        # either create_task or update_task; without candidates only create_task fits.
        call_tools = (
            [create_tool, extract_tools[-1]] if candidate_task_ids else [create_tool]
        ) if is_last else extract_tools
        resp = await chat(
            messages,
            settings,
            system_prompt=EXTRACT_FIELDS_SYSTEM_PROMPT,
            tools=call_tools,
            force_tool=("any" if candidate_task_ids else "create_task") if is_last else None,
        )
        log.debug(
            "llm response · raw=%s attempt=%d stop_reason=%s input_tokens=%s output_tokens=%s",
            raw.id, attempt, resp.stop_reason,
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

        tool_uses = list(resp.tool_calls)
        if not tool_uses:
            if is_last:
                raise ExtractionFailed("agent did not call a task tool during field extraction", trace)
            messages.append(assistant_message(resp))
            messages.append(user_message("Call create_task or update_task now. Do not reply in prose."))
            continue

        # Run lookups and event edits before accepting a terminal task action.
        search_uses = [tu for tu in tool_uses if tu.name in {
            "search_notes", "find_calendar_events", "get_event_details",
            "update_event", "research_input_link",
        }]
        results = []
        for tu in search_uses:
            tin = tu.input or {}
            changed_state = False
            if tu.name == "search_notes":
                out = await run_search_notes(session, str(tin.get("query") or ""))
            elif tu.name == "research_input_link":
                if researched_link or not research_enabled:
                    out = "Link research already attempted. Decide from the available facts."
                else:
                    researched_link = True
                    extract_tools = [tool for tool in extract_tools if tool["name"] != "research_input_link"]
                    out, web_trace = await research_link(
                        source_url,
                        settings,
                        task_title=provisional_task_title(raw),
                        task_context=raw.content,
                    )
                    out = out or "No page facts could be verified. Decide from the input."
                    trace["web_search"] = web_trace or {
                        "url": source_url, "status": "failed",
                    }
            elif tu.name == "find_calendar_events":
                out = await run_find_calendar_events(
                    session,
                    query=tin.get("query"),
                    time_min=tin.get("time_min"),
                    time_max=tin.get("time_max"),
                )
            elif tu.name == "get_event_details":
                out = await run_get_event_details(
                    session,
                    event_id=str(tin.get("event_id") or ""),
                    calendar_id=str(tin.get("calendar_id") or ""),
                )
            else:
                out, updated_id = await run_update_event(
                    session,
                    event_id=str(tin.get("event_id") or ""),
                    summary=tin.get("summary"),
                    start=tin.get("start"),
                    end=tin.get("end"),
                    description=tin.get("description"),
                    location=tin.get("location"),
                )
                changed_state = updated_id is not None
                if updated_id:
                    trace.setdefault("events_updated", []).append(updated_id)
            iter_log.setdefault("tool_results", []).append(
                _tool_result_entry(
                    tu.name,
                    tin,
                    out,
                    status=(
                        "failed" if tu.name == "research_input_link"
                        and trace.get("web_search", {}).get("status") != "success"
                        else "success"
                    ),
                    changed_state=changed_state,
                )
            )
            results.append(tool_result_message(tu, out))

        if any(tu.name == "research_input_link" for tu in search_uses):
            for tu in tool_uses:
                if tu.name in {"create_task", "update_task"}:
                    results.append(tool_result_message(
                        tu, "Decide again after reading the link research result."
                    ))
            messages.append(assistant_message(resp))
            messages.extend(results)
            continue

        terminal_use = next(
            (tu for tu in tool_uses if tu.name in {"create_task", "update_task"}),
            None,
        )
        if terminal_use is not None:
            payload = dict(terminal_use.input or {})
            if terminal_use.name != "create_task":
                target_id = str(payload.get("existing_task_id") or "")
                if target_id not in candidate_task_ids:
                    raise ValueError(f"Agent selected a task outside the candidate set: {target_id}")
                if not any(
                    payload.get(key) is not None
                    for key in ("title", "description", "estimation", "due_date", "location", "link", "label", "status")
                ):
                    raise ValueError("Manual update_task must change a task field or status")
                trace["action"] = terminal_use.name
            iter_log.setdefault("tool_results", []).append(
                _tool_result_entry(
                    terminal_use.name,
                    payload,
                    "extracted task fields",
                    changed_state=False,
                )
            )
            break

        if not search_uses:
            raise ExtractionFailed(
                f"unexpected tool calls during field extraction: "
                f"{[tu.name for tu in tool_uses]}", trace
            )

        # No terminal call yet — feed search_notes results back and continue.
        messages.append(assistant_message(resp))
        messages.extend(results)

    if not payload:
        raise ExtractionFailed("agent did not call a task tool during field extraction", trace)

    if "due_date" in payload:
        payload["due_date"] = normalize_agent_due_date(payload["due_date"])
    explicit_due = bool(payload.pop("due_date_is_explicit", False))
    payload, event_warning = apply_event_context(session, payload, explicit_due=explicit_due)
    if event_warning:
        trace["event_warning"] = event_warning
    if source_url and not payload.get("link"):
        payload["link"] = source_url
    # Notes ride on `create_task` but aren't task fields — persist and strip
    # them so callers can feed the payload straight into task creation. Skipped
    # entirely when note-harvesting is off (kotx), where the field isn't offered.
    notes = payload.pop("notes", None)
    if harvest_notes:
        await save_notes(session, raw.id, notes)
    # reason/confidence describe the decision, not the task — lift them onto the
    # trace and strip so they don't count as agent-extracted task fields.
    trace["reason"] = payload.pop("reason", None)
    trace["confidence"] = payload.pop("confidence", None)
    if trace.get("action") is None:
        _backstop_required(payload, raw_id=raw.id)
    if include_trace:
        return payload, trace
    return payload


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
        "result_markdown": str(summary or ""),
        "preview": _truncate_inline(summary, 200),
        "result_summary": _truncate_inline(summary, 500),
        "changed_state": changed_state,
        "artifact_refs": artifact_refs or [],
    }


def _tool_purpose(name: str, tool_input: dict[str, Any]) -> str:
    if name == "search_notes":
        return f"search notes for {_truncate_inline(str(tool_input.get('query') or ''), 80)}"
    if name == "create_task":
        return f"create task {_truncate_inline(str(tool_input.get('title') or ''), 80)}"
    if name == "update_task":
        return f"{name.replace('_', ' ')} {tool_input.get('existing_task_id') or ''}"
    return name


def _without_notes(tool: dict[str, Any]) -> dict[str, Any]:
    """A copy of a tool schema with its `notes` property removed, so the agent
    is never offered a place to write long-term memory."""
    params = tool["parameters"]
    props = {k: v for k, v in params["properties"].items() if k != "notes"}
    return {**tool, "parameters": {**params, "properties": props}}


def _truncate_inline(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _backstop_required(payload: dict, *, raw_id) -> None:
    """The tool schema marks `label` as required, but the LLM sometimes still
    ships a create_task call without it. Warn (and leave NULL) so the user can
    pick one manually."""
    if not payload.get("label"):
        log.warning(
            "agent skipped label · raw=%s — leaving NULL, user must assign", raw_id,
        )


def _build_extract_message(
    session: Session,
    raw,
    context_inputs=(),
    precedent_candidates: list[SimilarInput] | None = None,
) -> str:
    now_line = f"Current time: {now_iso(get_settings().user_timezone)}"
    precedent_lines = _precedent_lines(session, precedent_candidates or [])
    if not context_inputs:
        return "\n".join([now_line, *precedent_lines, *_render_input_lines(raw)])

    ordered = sorted([raw, *context_inputs], key=lambda r: r.received_at)
    lines = [
        now_line,
        *precedent_lines,
        "",
        f"This input is part of a conversation thread of {len(ordered)} "
        "messages, shown oldest first. Handle ONE task that captures the "
        "whole thread.",
    ]
    for i, item in enumerate(ordered, start=1):
        lines.append("")
        lines.append(f"===== Message {i} of {len(ordered)} =====")
        lines.extend(_render_input_lines(item))
    return "\n".join(lines)


def _precedent_lines(session: Session, candidates: list[SimilarInput]) -> list[str]:
    if not candidates:
        return []

    task_candidates: list[tuple[SimilarInput, Any]] = []
    for hit in candidates:
        if hit.task_id and hit.status in ("open", "closed"):
            task = tasks.get(session, hit.task_id)
            if task is not None:
                task_candidates.append((hit, task))
    rendered: list[str] = []
    for hit, task in task_candidates:
        rendered.extend(task_candidate_lines(hit, task))
    if not rendered:
        return []

    return [
        "",
        (
            "Candidate tasks (ranked by similarity). Use update_task with an "
            "existing_task_id when the input changes one of these tasks. "
            "Create a new task for separate work."
        ),
        *rendered,
    ]


def _render_input_lines(raw) -> list[str]:
    meta = raw.source_metadata or {}
    lines = [f"Source: {raw.source}"]
    for key in ("from", "to", "subject", "date", "thread_id", "account"):
        val = meta.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")
    lines.append("")
    lines.append("Body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return lines
