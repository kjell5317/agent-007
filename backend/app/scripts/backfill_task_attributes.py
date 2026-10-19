"""Backfill `work_type` / `effort` / `deadline_kind` on existing tasks.

Covers tasks due at most 7 days ago (open or done, not dismissed) that lack
a work type or effort, 20 tasks per call on the cheap background model. Only
missing values are filled, and a label's defaults beat the model for work
type and effort. `deadline_kind` is replaced only while it is still the
untouched default (`firm`, never overridden in the UI). A task the model
leaves out stays empty and is picked up by the next run.

    python -m app.scripts.backfill_task_attributes [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agent.helpers.attributes import DEADLINE_KINDS, EFFORTS, WORK_TYPES, valid_attributes
from app.agent.helpers.llm import chat, user_message
from app.agent.tools.schemas import DEADLINE_KIND_DOC, EFFORT_DOC, WORK_TYPE_DOC
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.db.clients import labels as labels_store
from app.db.clients import tasks as tasks_store
from app.db.models.scheduling import TaskOverride
from app.db.models.task import Task

log = logging.getLogger(__name__)

BATCH_SIZE = 20
LOOKBACK = timedelta(days=7)
# 20 entries plus Gemini's thinking overflow the default 1024-token cap.
MAX_TOKENS = 4096

CLASSIFY_TOOL = {
    "name": "classify_tasks",
    "description": "Record the work type, effort and deadline kind of every listed task.",
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer", "description": "The task's number in the list."},
                        "work_type": {
                            "type": "string", "enum": list(WORK_TYPES),
                            "description": WORK_TYPE_DOC,
                        },
                        "effort": {
                            "type": "string", "enum": list(EFFORTS), "description": EFFORT_DOC,
                        },
                        "deadline_kind": {
                            "type": "string", "enum": list(DEADLINE_KINDS),
                            "description": DEADLINE_KIND_DOC,
                        },
                    },
                    "required": ["n", "work_type", "effort", "deadline_kind"],
                },
            },
        },
        "required": ["tasks"],
    },
}

SYSTEM_PROMPT = (
    "Classify every numbered task from the user's task list. Call classify_tasks "
    "once with one entry per task, keyed by its number. Judge from the title, "
    "description, label, estimate and due date. A due date marked 'derived' was "
    "set by the app, not stated by the source; 'linked event' means the task "
    "prepares for that calendar event."
)


def batch_message(tasks: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for number, task in enumerate(tasks, start=1):
        lines.append(f"{number}. {task['title']}")
        facts = []
        if task.get("label"):
            facts.append(f"label: {task['label']}")
        if task.get("estimation"):
            facts.append(f"estimate: {task['estimation']} min")
        if task.get("due_date"):
            due = f"due: {task['due_date'].isoformat(timespec='minutes')}"
            if task.get("due_date_derived"):
                due += " (derived)"
            facts.append(due)
        if task.get("related_event"):
            facts.append("linked event")
        if facts:
            lines.append("   " + " · ".join(facts))
        description = " ".join((task.get("description") or "").split())
        if description:
            lines.append(f"   description: {description[:300]}")
    return "\n".join(lines)


def parse_classification(tool_input: Any, count: int) -> dict[int, dict[str, str]]:
    """`{task number: valid fields}`; unknown numbers and enum values are dropped."""
    items = tool_input.get("tasks") if isinstance(tool_input, Mapping) else None
    parsed: dict[int, dict[str, str]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, Mapping):
            continue
        try:
            number = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        fields = valid_attributes(item)
        if 1 <= number <= count and fields:
            parsed[number] = fields
    return parsed


def planned_update(
    current: Mapping[str, Any],
    suggested: Mapping[str, str],
    label_defaults: tuple[str | None, str | None],
    *,
    deadline_kind_overridden: bool,
) -> dict[str, str]:
    default_work_type, default_effort = label_defaults
    update: dict[str, str] = {}
    for field, default in (("work_type", default_work_type), ("effort", default_effort)):
        value = default or suggested.get(field)
        if current.get(field) is None and value:
            update[field] = value
    kind = suggested.get("deadline_kind")
    if (
        kind
        and kind != "firm"
        and (current.get("deadline_kind") or "firm") == "firm"
        and not deadline_kind_overridden
    ):
        update["deadline_kind"] = kind
    return update


def _snapshot(task: Task) -> dict[str, Any]:
    return {
        "title": task.title,
        "description": task.description,
        "label": task.label,
        "estimation": task.estimation,
        "due_date": task.due_date,
        "due_date_derived": task.due_date_derived or task.related_event_due_derived,
        "related_event": bool(task.related_event_id),
        "work_type": task.work_type,
        "effort": task.effort,
        "deadline_kind": task.deadline_kind,
    }


def candidates(session: Session) -> list[Task]:
    since = datetime.now(timezone.utc) - LOOKBACK
    rows = list(session.scalars(
        select(Task)
        .where(or_(Task.work_type.is_(None), Task.effort.is_(None)), Task.due_date >= since)
        .order_by(Task.created_at.desc())
    ))
    statuses = tasks_store.latest_status_for(session, [row.id for row in rows])
    return [row for row in rows if statuses.get(row.id, "open") != "not_task"]


def _deadline_kind_overridden(session: Session) -> set[uuid.UUID]:
    return set(session.scalars(
        select(TaskOverride.task_id)
        .where(TaskOverride.field == "deadline_kind", TaskOverride.source == "ui")
    ))


async def classify(
    snapshots: Sequence[Mapping[str, Any]], settings: Settings
) -> dict[int, dict[str, str]]:
    # Same cheap route as the subtask planner: Gemini Flash when configured.
    gemini = bool(settings.gemini_api_key)
    response = await chat(
        [user_message(batch_message(snapshots))],
        settings,
        system_prompt=SYSTEM_PROMPT,
        tools=[CLASSIFY_TOOL],
        force_tool="classify_tasks",
        provider="google" if gemini else None,
        model=settings.chat_llm_model if gemini else None,
        thinking_level=settings.chat_thinking_level if gemini else None,
        max_tokens=MAX_TOKENS,
        name="backfill-task-attributes",
    )
    call = next((c for c in response.tool_calls if c.name == "classify_tasks"), None)
    return parse_classification(call.input if call else None, len(snapshots))


async def backfill(
    session: Session, settings: Settings, *, dry_run: bool, limit: int | None
) -> int:
    tasks = candidates(session)[:limit]
    label_defaults = labels_store.defaults_by_name(session)
    locked = _deadline_kind_overridden(session)
    print(f"{len(tasks)} tasks to classify")
    updated = 0
    for start in range(0, len(tasks), BATCH_SIZE):
        batch = tasks[start:start + BATCH_SIZE]
        snapshots = [_snapshot(task) for task in batch]
        try:
            suggestions = await classify(snapshots, settings)
        except Exception:  # noqa: BLE001 — the batch stays unfilled for the next run
            log.exception("classification failed · batch starting at %d", start)
            continue
        for number, (task, snapshot) in enumerate(zip(batch, snapshots), start=1):
            if number not in suggestions:
                continue
            update = planned_update(
                snapshot,
                suggestions[number],
                label_defaults.get(task.label or "", (None, None)),
                deadline_kind_overridden=task.id in locked,
            )
            if not update:
                continue
            updated += 1
            print(f"{task.id} {task.title[:60]!r}: {update}")
            if not dry_run:
                tasks_store.update(session, task.id, **update)
        if not dry_run:
            session.commit()
    return updated


async def _run(dry_run: bool, limit: int | None) -> int:
    with SessionLocal() as session:
        updated = await backfill(session, get_settings(), dry_run=dry_run, limit=limit)
        if dry_run:
            session.rollback()
        return updated


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="call the model and print, but write nothing"
    )
    parser.add_argument("--limit", type=int, default=None, help="classify at most N tasks")
    args = parser.parse_args(argv)
    logging.basicConfig()
    updated = asyncio.run(_run(args.dry_run, args.limit))
    print(f"{'would update' if args.dry_run else 'updated'} {updated} tasks")


if __name__ == "__main__":
    main()
