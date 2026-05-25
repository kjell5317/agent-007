"""Field-extraction agent for manually-promoted inputs.

Used by the promote-input endpoint when the user has already decided "this
is a task" but hasn't supplied the structured fields. The agent's only job
is to populate `create_task` — no dedup, no candidates, no `mark_not_task`.
"""

from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic

from app.agent.runner.llm import cached_system, cached_tools, create_message
from app.agent.runner.text import now_iso, parse_iso
from app.agent.tools import NEW_INPUT_TOOLS
from app.config import get_settings

log = logging.getLogger(__name__)

_EXTRACT_FIELDS_SYSTEM_PROMPT = """\
You are extracting structured task fields from a raw input the user has
explicitly chosen to promote to a task. Do NOT second-guess the decision —
your only job is to populate `create_task` accurately:

- title: short, imperative.
- estimation: minutes; required, best-guess.
- due_date: ISO 8601; required — use the explicit deadline if stated,
  otherwise a reasonable best-guess based on urgency. The user message
  begins with a "Current time:" line; the due_date MUST be at or after
  that time — never pick a date in the past.
- label: required if the `label` field is part of the tool schema —
  pick the single best-fitting value from the enum. The user has already
  committed to this being a task, so pick the closest match even if the
  fit is loose.
- ai_doable: required — `yes` / `no` / `unsure`, as described in the
  tool schema.
- description, location, link: include when supported by the input.

Call `create_task` exactly once. Do not narrate.
"""


async def extract_task_fields(raw) -> dict[str, Any]:
    """Ask the LLM to extract task fields from a raw input."""
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_msg = _build_extract_message(raw)
    create_tool = next(t for t in NEW_INPUT_TOOLS if t["name"] == "create_task")

    log.info("llm call · branch=extract_fields raw=%s", raw.id)
    resp = await create_message(
        client, settings,
        system=cached_system(_EXTRACT_FIELDS_SYSTEM_PROMPT),
        tools=cached_tools([create_tool]),
        tool_choice={"type": "tool", "name": "create_task"},
        messages=[{"role": "user", "content": user_msg}],
    )
    log.debug(
        "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
        raw.id, resp.stop_reason,
        getattr(resp.usage, "input_tokens", "?"),
        getattr(resp.usage, "output_tokens", "?"),
    )

    tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    if not tool_uses:
        raise RuntimeError("agent did not call create_task during field extraction")
    payload = dict(tool_uses[0].input or {})
    if "due_date" in payload:
        payload["due_date"] = parse_iso(str(payload["due_date"]))
    return payload


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
