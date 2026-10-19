"""Scheduling attributes (`work_type`, `effort`, `deadline_kind`) on agent
task tool calls: enum validation at the tool boundary, plus the defaults for
a new task when the agent leaves them out."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, get_args

from app.db.schemas.task import DeadlineKind, Effort, WorkType

WORK_TYPES: tuple[str, ...] = get_args(WorkType)
EFFORTS: tuple[str, ...] = get_args(Effort)
DEADLINE_KINDS: tuple[str, ...] = get_args(DeadlineKind)
ATTRIBUTE_FIELDS = ("work_type", "effort", "deadline_kind")

_ALLOWED = {"work_type": WORK_TYPES, "effort": EFFORTS, "deadline_kind": DEADLINE_KINDS}


def valid_attributes(tool_input: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, allowed in _ALLOWED.items():
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip().lower() in allowed:
            out[field] = value.strip().lower()
    return out


def default_deadline_kind(*, explicit_due: bool, related_event: bool) -> str:
    if explicit_due:
        return "firm"
    # Preparation for a linked event is pointless once the event has started,
    # even when the agent's own guess sits earlier than the event.
    return "hard" if related_event else "soft"


def new_task_attributes(
    tool_input: Mapping[str, Any],
    label_defaults: Mapping[str, tuple[str | None, str | None]],
    *,
    label: str | None,
    explicit_due: bool,
    related_event: bool,
) -> dict[str, str | None]:
    picked = valid_attributes(tool_input)
    default_work_type, default_effort = label_defaults.get(label or "", (None, None))
    return {
        "work_type": picked.get("work_type") or default_work_type,
        "effort": picked.get("effort") or default_effort,
        "deadline_kind": picked.get("deadline_kind")
        or default_deadline_kind(explicit_due=explicit_due, related_event=related_event),
    }
