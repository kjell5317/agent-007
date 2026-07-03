"""Field-extraction agent for manually-promoted inputs.

Used by the promote-input endpoint when the user has already decided "this
is a task" but hasn't supplied the structured fields. The agent's only job
is to populate `create_task` — no dedup, no candidates, no `mark_not_task`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agent.prompts import EXTRACT_FIELDS_SYSTEM_PROMPT
from app.agent.helpers.llm import (
    LLMMessage,
    MAX_TOOL_ITERATIONS,
    assistant_message,
    chat,
    tool_result_message,
    user_message,
)
from app.agent.tools.notes_lookup import run_search_notes, save_notes
from app.agent.helpers.text import normalize_agent_due_date, now_iso
from app.agent.tools import NEW_INPUT_TOOLS
from app.config import get_settings

log = logging.getLogger(__name__)


async def extract_task_fields(session: Session, raw, *, context_inputs=()) -> dict[str, Any]:
    """Ask the LLM to extract task fields from a raw input.

    `context_inputs` are sibling raw_inputs from the same thread/follow-up
    group. When present, the agent sees the whole conversation (oldest first)
    and is told to produce ONE task capturing it.

    Multi-step loop so the model can call `search_notes` before finalizing
    with `create_task`. The last iteration forces `create_task` via tool_choice
    so we always end with a populated payload."""
    settings = get_settings()

    user_msg = _build_extract_message(raw, context_inputs)
    create_tool = next(t for t in NEW_INPUT_TOOLS if t["name"] == "create_task")
    search_tool = next(t for t in NEW_INPUT_TOOLS if t["name"] == "search_notes")
    extract_tools = [search_tool, create_tool]

    messages: list[LLMMessage] = [user_message(user_msg)]
    log.info("llm call · branch=extract_fields raw=%s", raw.id)

    payload: dict[str, Any] = {}
    for attempt in range(MAX_TOOL_ITERATIONS - 1):
        is_last = attempt == MAX_TOOL_ITERATIONS - 2
        # On the final iteration, force create_task so we never finish without
        # a finalized payload — earlier iterations let the model pick freely
        # so it can chain search_notes lookups first.
        resp = await chat(
            messages,
            settings,
            system_prompt=EXTRACT_FIELDS_SYSTEM_PROMPT,
            tools=extract_tools,
            force_tool="create_task" if is_last else None,
        )
        log.debug(
            "llm response · raw=%s attempt=%d stop_reason=%s input_tokens=%s output_tokens=%s",
            raw.id, attempt, resp.stop_reason,
            resp.usage.get("input_tokens", "?"),
            resp.usage.get("output_tokens", "?"),
        )

        tool_uses = list(resp.tool_calls)
        if not tool_uses:
            raise RuntimeError(
                "agent did not call any tool during field extraction"
            )

        create_use = next((tu for tu in tool_uses if tu.name == "create_task"), None)
        if create_use is not None:
            payload = dict(create_use.input or {})
            break

        # No terminal call yet — execute any search_notes calls and continue.
        search_uses = [tu for tu in tool_uses if tu.name == "search_notes"]
        if not search_uses:
            raise RuntimeError(
                f"unexpected tool calls during field extraction: "
                f"{[tu.name for tu in tool_uses]}"
            )
        results = []
        for tu in search_uses:
            out = await run_search_notes(
                session, str((tu.input or {}).get("query") or ""),
            )
            results.append(tool_result_message(tu, out))
        messages.append(assistant_message(resp))
        messages.extend(results)

    if "due_date" in payload:
        payload["due_date"] = normalize_agent_due_date(payload["due_date"])
    # Notes ride on `create_task` but aren't task fields — persist and strip
    # them so callers can feed the payload straight into task creation.
    await save_notes(session, raw.id, payload.pop("notes", None))
    _backstop_required(payload, raw_id=raw.id)
    return payload


def _backstop_required(payload: dict, *, raw_id) -> None:
    """The tool schema marks `label` as required, but the LLM sometimes still
    ships a create_task call without it. Warn (and leave NULL) so the user can
    pick one manually."""
    if not payload.get("label"):
        log.warning(
            "agent skipped label · raw=%s — leaving NULL, user must assign", raw_id,
        )


def _build_extract_message(raw, context_inputs=()) -> str:
    now_line = f"Current time: {now_iso(get_settings().user_timezone)}"
    if not context_inputs:
        return "\n".join([now_line, *_render_input_lines(raw)])

    ordered = sorted([raw, *context_inputs], key=lambda r: r.received_at)
    lines = [
        now_line,
        "",
        f"This input is part of a conversation thread of {len(ordered)} "
        "messages, shown oldest first. Create ONE task that captures the "
        "whole thread.",
    ]
    for i, item in enumerate(ordered, start=1):
        lines.append("")
        lines.append(f"===== Message {i} of {len(ordered)} =====")
        lines.extend(_render_input_lines(item))
    return "\n".join(lines)


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
