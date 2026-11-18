"""`tool_results` entries the new-input and manual agents write into a raw
input's `agent_trace`."""

from __future__ import annotations

from typing import Any

from app.agent.helpers.precedents import truncate_inline


def tool_result_entry(
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
        "preview": truncate_inline(summary, 200),
        "result_summary": truncate_inline(summary, 500),
        "changed_state": changed_state,
        "artifact_refs": artifact_refs or [],
    }


def _tool_purpose(name: str, tool_input: dict[str, Any]) -> str:
    if name == "search_notes":
        return f"search notes for {truncate_inline(str(tool_input.get('query') or ''), 80)}"
    if name == "find_calendar_events":
        return "find calendar conflicts"
    if name == "create_event":
        return f"create calendar event {truncate_inline(str(tool_input.get('summary') or ''), 80)}"
    if name == "update_event":
        return f"update calendar event {truncate_inline(str(tool_input.get('event_id') or ''), 80)}"
    if name == "create_task":
        return f"create task {truncate_inline(str(tool_input.get('title') or ''), 80)}"
    if name == "update_task":
        return "update existing task"
    if name == "mark_not_task":
        return "mark input as not a task"
    if name == "no_change":
        return "leave existing task unchanged"
    return name
