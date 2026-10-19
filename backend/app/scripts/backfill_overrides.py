"""Backfill `task_overrides` (source=backfill) from history.

For every task due at most `LOOKBACK` (7 days) ago, the agent's last value per field — taken from the task tool
calls in `raw_inputs.agent_trace` and from chat `create_task` / `update_task`
calls — is compared with the task's current value; each difference becomes
one row. Noisy by nature (calendar resizes and agent follow-ups changed
fields too), which is why learning down-weights backfill rows. Tasks that
already have backfill rows are skipped, so re-running is safe.

    python -m app.scripts.backfill_overrides [--dry-run]
"""

from __future__ import annotations

import argparse
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.helpers.attributes import ATTRIBUTE_FIELDS, valid_attributes
from app.agent.helpers.text import normalize_agent_due_date
from app.db import SessionLocal
from app.db.clients import overrides
from app.db.models.chat import ChatConversation
from app.db.models.raw_input import RawInput
from app.db.models.task import Task

TRACKED_FIELDS = ("due_date", "estimation", "label", *ATTRIBUTE_FIELDS)
SOURCE = "backfill"

_TASK_TOOLS = frozenset({"create_task", "update_task"})
_TERMINAL_TOOLS = _TASK_TOOLS | {"no_change", "mark_not_task", "close_subtasks"}
_SAME_INSTANT = timedelta(minutes=1)
LOOKBACK = timedelta(days=7)


@dataclass(frozen=True)
class AgentCall:
    task_id: str
    tool_input: Mapping[str, Any]
    raw_input_id: uuid.UUID | None = None


def trace_calls(
    trace: Any, *, raw_input_id: uuid.UUID | None = None, raw_task_id: str | None = None
) -> list[AgentCall]:
    """The task tool calls of one `agent_trace` that took effect, oldest first.

    Only the last response carrying a terminal tool ran, and only its first
    terminal call — an earlier one was re-asked after a lookup. A manual
    override nests a later decision under `manual_override`.
    """
    if not isinstance(trace, Mapping):
        return []
    responses = [
        iteration.get("blocks")
        for iteration in trace.get("iterations") or []
        if isinstance(iteration, Mapping)
    ]
    responses.append(trace.get("blocks"))
    executed = None
    for blocks in responses:
        terminal = next(
            (
                block for block in blocks or []
                if isinstance(block, Mapping)
                and block.get("type") == "tool_use"
                and block.get("name") in _TERMINAL_TOOLS
            ),
            None,
        )
        if terminal is not None:
            executed = terminal

    calls: list[AgentCall] = []
    if executed is not None and executed.get("name") in _TASK_TOOLS:
        tool_input = executed.get("input") if isinstance(executed.get("input"), Mapping) else {}
        if executed["name"] == "create_task":
            task_id = trace.get("task_id") or raw_task_id
        else:
            task_id = (
                tool_input.get("existing_task_id") or trace.get("existing_task_id")
                or trace.get("task_id") or raw_task_id
            )
        if task_id:
            calls.append(AgentCall(str(task_id), tool_input, raw_input_id))
    return calls + trace_calls(
        trace.get("manual_override"), raw_input_id=raw_input_id, raw_task_id=raw_task_id
    )


def chat_calls(messages: Any) -> list[AgentCall]:
    """Successful task tool calls in a persisted chat conversation, in order."""
    calls: list[AgentCall] = []
    for message in messages if isinstance(messages, list) else []:
        tools = message.get("tools") if isinstance(message, Mapping) else None
        for tool in tools if isinstance(tools, list) else []:
            if (
                not isinstance(tool, Mapping)
                or tool.get("name") not in _TASK_TOOLS
                or tool.get("status") != "success"
            ):
                continue
            params = tool.get("params") if isinstance(tool.get("params"), Mapping) else {}
            task_id = params.get("task_id") or next(
                (
                    ref.removeprefix("task:") for ref in tool.get("artifact_refs") or []
                    if isinstance(ref, str) and ref.startswith("task:")
                ),
                None,
            )
            if task_id:
                calls.append(AgentCall(str(task_id), params))
    return calls


def override_rows(task: Mapping[str, Any], calls: Sequence[AgentCall]) -> list[dict[str, Any]]:
    """One row per tracked field whose last agent value differs from the task's
    current value. `task` holds the current field values plus the flags
    `related_event_due_derived`, `due_date_derived` and `is_container`."""
    last: dict[str, tuple[Any, uuid.UUID | None]] = {}
    for call in calls:
        for field in TRACKED_FIELDS:
            value = _agent_value(field, call.tool_input.get(field))
            if value is not None:
                last[field] = (value, call.raw_input_id)

    owned = _system_owned(task)
    rows = []
    for field, (agent_value, raw_input_id) in last.items():
        current = task.get(field)
        if field in owned or _same(field, agent_value, current):
            continue
        rows.append({
            "field": field,
            "agent_value": agent_value,
            "user_value": current,
            "raw_input_id": raw_input_id,
        })
    return rows


def _agent_value(field: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    if field == "due_date":
        try:
            parsed = normalize_agent_due_date(value)
        except (TypeError, ValueError):
            return None
        return _aware(parsed)
    if field == "estimation":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if field in ATTRIBUTE_FIELDS:
        return valid_attributes({field: value}).get(field)
    return str(value)


def _aware(value: datetime) -> datetime:
    # A naive timestamp was stored in the database session's zone, UTC here.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _system_owned(task: Mapping[str, Any]) -> set[str]:
    # Values the app derived itself are not user corrections: the due date
    # follows the linked event or the parent's timeline until the user edits
    # it, and a container's duration is the sum of its subtasks.
    owned: set[str] = set()
    if task.get("related_event_due_derived") or task.get("due_date_derived"):
        owned.add("due_date")
    if task.get("is_container"):
        owned.add("estimation")
    return owned


def _same(field: str, agent_value: Any, current: Any) -> bool:
    if field == "due_date":
        return current is not None and abs(agent_value - _aware(current)) < _SAME_INSTANT
    return agent_value == current


def _snapshot(task: Task) -> dict[str, Any]:
    return {
        **{field: getattr(task, field) for field in TRACKED_FIELDS},
        "related_event_due_derived": task.related_event_due_derived,
        "due_date_derived": task.due_date_derived,
        "is_container": task.is_container,
    }


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def collect_calls(session: Session) -> dict[uuid.UUID, list[AgentCall]]:
    timed: list[tuple[datetime, int, AgentCall]] = []
    raw_rows = session.execute(
        select(RawInput.id, RawInput.task_id, RawInput.received_at, RawInput.agent_trace)
    )
    for raw_id, raw_task_id, received_at, trace in raw_rows:
        for call in trace_calls(
            trace, raw_input_id=raw_id, raw_task_id=str(raw_task_id) if raw_task_id else None
        ):
            timed.append((received_at, len(timed), call))
    # Chat messages carry no timestamps; the conversation start orders them.
    for created_at, messages in session.execute(
        select(ChatConversation.created_at, ChatConversation.messages)
    ):
        for call in chat_calls(messages):
            timed.append((created_at, len(timed), call))

    by_task: dict[uuid.UUID, list[AgentCall]] = defaultdict(list)
    for _, _, call in sorted(timed, key=lambda item: item[:2]):
        task_id = _uuid(call.task_id)
        if task_id is not None:
            by_task[task_id].append(call)
    return by_task


def backfill(session: Session, *, dry_run: bool) -> Counter[str]:
    by_task = collect_calls(session)
    since = datetime.now(timezone.utc) - LOOKBACK
    tasks = list(session.scalars(
        select(Task).where(Task.id.in_(list(by_task)), Task.due_date >= since)
    ))
    written: Counter[str] = Counter()
    for task in tasks:
        if overrides.exists_for_source(session, task.id, SOURCE):
            continue
        for row in override_rows(_snapshot(task), by_task[task.id]):
            written[row["field"]] += 1
            if dry_run:
                print(
                    f"{task.id} {task.title[:60]!r}: {row['field']} "
                    f"{row['agent_value']!s} -> {row['user_value']!s}"
                )
                continue
            overrides.add(session, task_id=task.id, source=SOURCE, **row)
    print(f"{len(tasks)} tasks with agent calls")
    return written


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print rows without writing")
    args = parser.parse_args(argv)
    with SessionLocal() as session:
        written = backfill(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {sum(written.values())} override rows: {dict(written)}")


if __name__ == "__main__":
    main()
