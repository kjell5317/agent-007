"""Shared terminal-action dispatch for the agent flows.

Both the thread-follow-up flow and the new-input duplicate-handling flow end
by acting on an *existing* task: `update_task` (edit fields and/or drive the
lifecycle via its `status`) or `no_change` (leave it alone). The side effects
are identical — reschedule/notify on update, close/reopen on a status change —
so they live here and both runners call the same function.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.agent.helpers.text import parse_iso
from app.db.models.task import Task
from app.services.notify import notify_agent_task_closed, notify_agent_task_updated
from app.services.task.close import close_task as close_task_svc
from app.services.task.reopen import reopen_task as reopen_task_svc
from app.services.task.update import PLAN_TRIGGER_FIELDS, update_task as update_task_svc

# Fields carried on the tool variants that aren't task columns and must be
# stripped before patching (`status` drives the lifecycle, not a column).
_NON_PATCH_FIELDS = frozenset({"existing_task_id", "reason", "confidence", "status"})


async def apply_task_action(
    session: Session, task: Task, tool_name: str, tu_input: dict[str, Any]
) -> dict[str, Any]:
    """Apply `update_task` (fields and/or a `status` lifecycle change) or
    `no_change` to `task`.

    Returns a trace fragment for the caller to merge into its own trace.
    Unknown tool names yield an `unknown_tool:` outcome rather than raising.
    """
    if tool_name == "update_task":
        status = tu_input.get("status")
        patch = {
            k: v for k, v in tu_input.items()
            if v is not None and k not in _NON_PATCH_FIELDS
        }
        if "due_date" in patch:
            patch["due_date"] = parse_iso(str(patch["due_date"]))

        outcome = "updated"
        # Reopen first so the calendar event exists before any field edits sync
        # to it; close last so we don't update an event we're about to delete.
        if status == "open":
            try:
                await reopen_task_svc(session, task.id)
            except LookupError:
                pass
            outcome = "reopened"

        if patch:
            try:
                updated = await update_task_svc(session, task.id, patch)
            except LookupError:
                updated = None
            # Plan-relevant edits route through `schedule_task`, which fires its
            # own "Rescheduled" notification with the new slot. Firing the
            # generic "Agent updated" too would replace it via the shared task
            # tag and lose the slot, so only notify for non-plan edits.
            if updated is not None and not (patch.keys() & PLAN_TRIGGER_FIELDS):
                await notify_agent_task_updated(updated, changes=patch)

        if status == "closed":
            try:
                await close_task_svc(session, task.id)
            except LookupError:
                pass
            await notify_agent_task_closed(task)
            outcome = "closed"

        return {"outcome": outcome, "status_change": status}

    if tool_name == "no_change":
        return {"outcome": "no_change", "confidence": tu_input.get("confidence")}

    return {"outcome": f"unknown_tool:{tool_name}"}
