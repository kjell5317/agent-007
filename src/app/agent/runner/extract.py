"""Field-extraction agent for manually-promoted inputs.

Used by the promote-input endpoint when the user has already decided "this
is a task" but hasn't supplied the structured fields. The agent's only job
is to populate `create_task` — no dedup, no candidates, no `mark_not_task`.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from sqlalchemy.orm import Session

from app.agent.runner.llm import (
    MAX_TOOL_ITERATIONS,
    cached_system,
    cached_tools,
    create_message,
)
from app.agent.runner.notes_lookup import run_search_notes
from app.agent.runner.text import now_iso, parse_iso
from app.agent.tools import NEW_INPUT_TOOLS
from app.config import get_settings

log = logging.getLogger(__name__)

_EXTRACT_FIELDS_SYSTEM_PROMPT = """\
You are extracting structured task fields from a raw input the user has
explicitly chosen to promote to a task. Do NOT second-guess the decision —
your only job is to populate `create_task` accurately.

The user already committed to "this is a task". Pick reasonable values even
when the fit is loose.

Every `create_task` call MUST include all five required fields below.
Omitting any one is a bug. Double-check before emitting the tool call.

REQUIRED fields:
    * title — very short, imperative. Start with the GitHub issue number if available.
    * estimation — minutes; always your best guess.
    * due_date — ISO 8601 with timezone. Use the explicit deadline if stated,
        otherwise a reasonable best-guess based on urgency. The user message
        begins with a "Current time:" line; due_date must be at or after that
        time. Round to 5-minute steps.
    * ai_doable — one of `yes` / `no` / `unsure`. See the tool schema.
    * label — pick the single best-fitting value from the enum. If nothing
        plausibly fits, call `mark_not_task` instead.

Optional: description, location (home is possible), link (most relevant source URL).

You also have one non-terminal tool:

- `search_notes(query)` — look up the agent's long-term memory (facts
  saved from past `mark_not_task` inputs). Call this before deciding when
  the current input mentions a person, project, account, or fact you might
  have recorded earlier. You may call it more than once. After searching
  you still need to call one of the terminal tools above to finish.

The user message may include a "Past similar inputs" section listing prior
decisions on near-duplicate inputs. Treat these as strong precedent.

If GitHub or Notion MCP tools are available and the input references something
opaque — a GitHub issue/PR number, a Notion page title or ID — and resolving
that reference would meaningfully change the title, description, or due_date,
call the relevant MCP tool first. Skip the lookup when the input already
stands on its own; do not chase context speculatively.

Call `create_task` exactly once. Do not narrate.
"""


async def extract_task_fields(session: Session, raw) -> dict[str, Any]:
    """Ask the LLM to extract task fields from a raw input.

    Multi-step loop so the model can call `search_notes` (and any configured
    MCP tools — those run server-side) before finalizing with `create_task`.
    The last iteration forces `create_task` via tool_choice so we always end
    with a populated payload."""
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_msg = _build_extract_message(raw)
    create_tool = next(t for t in NEW_INPUT_TOOLS if t["name"] == "create_task")
    search_tool = next(t for t in NEW_INPUT_TOOLS if t["name"] == "search_notes")
    extract_tools = [search_tool, create_tool]

    messages: list[MessageParam] = [{"role": "user", "content": user_msg}]
    log.info("llm call · branch=extract_fields raw=%s", raw.id)

    payload: dict[str, Any] = {}
    for attempt in range(MAX_TOOL_ITERATIONS - 1):
        is_last = attempt == MAX_TOOL_ITERATIONS - 2
        # On the final iteration, force create_task so we never finish without
        # a finalized payload — earlier iterations let the model pick freely
        # so it can chain search_notes lookups first.
        tool_choice: dict[str, Any] = (
            {"type": "tool", "name": "create_task"}
            if is_last
            else {"type": "auto"}
        )
        resp = await create_message(
            client, settings,
            system=cached_system(_EXTRACT_FIELDS_SYSTEM_PROMPT),
            tools=cached_tools(extract_tools),
            tool_choice=tool_choice,
            messages=messages,
        )
        log.debug(
            "llm response · raw=%s attempt=%d stop_reason=%s input_tokens=%s output_tokens=%s",
            raw.id, attempt, resp.stop_reason,
            getattr(resp.usage, "input_tokens", "?"),
            getattr(resp.usage, "output_tokens", "?"),
        )

        tool_uses = [
            b for b in resp.content if getattr(b, "type", None) == "tool_use"
        ]
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
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": out,
            })
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": results})

    if "due_date" in payload:
        payload["due_date"] = parse_iso(str(payload["due_date"]))
    _backstop_required(payload, raw_id=raw.id)
    return payload


def _backstop_required(payload: dict, *, raw_id) -> None:
    """The tool schema marks `ai_doable` and `label` as required, but the LLM
    sometimes still ships a create_task call without them. Fill in a sane
    default for ai_doable; warn (and leave NULL) for label so the user can
    pick one manually."""
    if not payload.get("ai_doable"):
        log.warning(
            "agent skipped ai_doable · raw=%s — defaulting to 'unsure'", raw_id,
        )
        payload["ai_doable"] = "unsure"
    if not payload.get("label"):
        log.warning(
            "agent skipped label · raw=%s — leaving NULL, user must assign", raw_id,
        )


def _build_extract_message(raw) -> str:
    meta = raw.source_metadata or {}
    lines = [f"Current time: {now_iso()}", f"Source: {raw.source}"]
    for key in ("from", "to", "subject", "date", "thread_id", "account"):
        val = meta.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")
    lines.append("")
    lines.append("Body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)
