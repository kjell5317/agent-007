"""Turn a broad task into linked, schedulable pieces."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.agent.helpers.llm import chat, user_message
from app.config import get_settings
from app.db.clients import tasks as tasks_store
from app.db.models.raw_input import RawInput
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate
from app.events import publish_task
from app.services.calendar import delete_task_event
from app.services.plan import schedule_task

log = logging.getLogger(__name__)

_CONJUNCTION = re.compile(
    r"(?iu)(?<!\w)(?:and|und|et|y|e|en|och|og|ja|i|a|ve|és|și|si|&|\+)(?!\w)|[と及和与＆]"
)
_PLAN_TOOL = {
    "name": "propose_subtasks",
    "description": "Propose 2 to 6 grounded subtasks, or none when the task cannot be split faithfully.",
    "parameters": {
        "type": "object",
        "properties": {
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "source_excerpt": {"type": "string", "description": "Exact excerpt from the task title or description that grounds this step."},
                        "estimation": {"type": "integer"},
                        "depends_on": {"type": "integer", "description": "Zero-based earlier subtask index, or omit."},
                        "due_date": {"type": "string", "description": "ISO deadline only when the source explicitly gives one for this step."},
                    },
                    "required": ["title", "estimation", "source_excerpt"],
                },
            },
        },
        "required": ["subtasks"],
    },
}


@dataclass(frozen=True)
class SubtaskPlan:
    title: str
    description: str | None
    estimation: int
    depends_on: int | None = None
    due_date: datetime | None = None


def needs_split(task: Task) -> bool:
    """Estimate is minutes; conjunctions are matched as words in the title."""
    return bool(
        (getattr(task, "estimation", None) or 0) > 120
        or _CONJUNCTION.search(getattr(task, "title", "") or "")
    )


async def propose_subtasks(task: Task) -> list[SubtaskPlan]:
    settings = get_settings()
    short_task = task.estimation is None or task.estimation <= 120
    response = await chat(
        [user_message(
            f"Title: {task.title}\nDescription: {task.description or ''}\n"
            f"Estimate: {task.estimation or 'unknown'} minutes\n"
            f"Overall deadline: {task.due_date.isoformat()}\n"
            f"Related event: {task.related_event_calendar_id or ''}:{task.related_event_id or ''}"
        )],
        settings,
        system_prompt=(
            "Split the task into 2–6 concrete, independently completable steps, "
            "each estimated at most 120 minutes. Preserve the source's exact "
            "intent, objects, and scope. Do not add unrelated work. For each step, "
            "give a verbatim source_excerpt from the title or description that "
            "supports it. "
            + (
                "This task is at most two hours or has no estimate: only separate actions explicitly "
                "stated in its title or description. Do not infer preparation, "
                "research, review, follow-up, or other unstated steps. Return an "
                "empty subtasks array if fewer than two distinct actions are stated. "
                if short_task else
                "This task is over two hours: you may infer "
                "necessary intermediate steps, but ground each one in a concrete "
                "detail or outcome from the title or description. Cover the stated "
                "work without adding speculative deliverables or stakeholders. "
            )
            + "Use the overall deadline. If one step must "
            "finish before another, set depends_on to the earlier step's "
            "zero-based index. Leave it out for parallel steps. Set a step's "
            "due_date only if the source explicitly gives that step a separate "
            "deadline. Do not invent event dates. Call propose_subtasks."
        ),
        tools=[_PLAN_TOOL],
        force_tool="propose_subtasks",
        provider="google" if settings.gemini_api_key else None,
        model=settings.chat_llm_model if settings.gemini_api_key else None,
        name="split-task-plan",
    )
    call = next((c for c in response.tool_calls if c.name == "propose_subtasks"), None)
    if call is None:
        raise ValueError("Subtask planner returned no plan")
    raw = call.input.get("subtasks")
    if not isinstance(raw, list) or (len(raw) != 0 and not 2 <= len(raw) <= 6):
        raise ValueError("Subtask planner must return 0 or 2–6 steps")
    plans: list[SubtaskPlan] = []
    source_text = f"{task.title}\n{task.description or ''}".casefold()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("Invalid subtask plan")
        title = str(item.get("title") or "").strip()[:512]
        excerpt = str(item.get("source_excerpt") or "").strip()
        estimate = int(item.get("estimation") or 0)
        dependency = item.get("depends_on")
        if not title or not 5 <= estimate <= 120:
            raise ValueError("Subtask title or estimate is invalid")
        if not excerpt or excerpt.casefold() not in source_text:
            raise ValueError("Subtask lacks source evidence")
        if dependency is not None:
            dependency = int(dependency)
            if not 0 <= dependency < index:
                raise ValueError("A subtask may depend only on an earlier step")
        due = None
        if item.get("due_date"):
            due = datetime.fromisoformat(str(item["due_date"]).replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        plans.append(SubtaskPlan(
            title=title,
            description=str(item.get("description") or "").strip() or None,
            estimation=estimate,
            depends_on=dependency,
            due_date=due,
        ))
    return plans


def _deadlines(parent_due: datetime, plans: list[SubtaskPlan]) -> list[datetime]:
    due = [parent_due] * len(plans)
    cursor = parent_due
    for index in range(len(plans) - 1, -1, -1):
        due[index] = cursor
        cursor -= timedelta(minutes=plans[index].estimation + 15)
    return due


def reflow_children(session: Session, parent: Task, children: list[Task] | None = None) -> list[Task]:
    """Keep the final child at the parent deadline and space earlier children backwards."""
    children = children if children is not None else tasks_store.children(session, parent.id)
    cursor = parent.due_date
    for index in range(len(children) - 1, -1, -1):
        child = children[index]
        child.subtask_order = index
        child.due_date = cursor
        child.due_date_derived = True
        child.depends_on_task_id = children[index - 1].id if index else None
        cursor -= timedelta(minutes=(getattr(child, "estimation", None) or 30) + 15)
    parent.estimation = sum(getattr(child, "estimation", None) or 30 for child in children)
    session.commit()
    publish_task(session, parent.id)
    for child in children:
        publish_task(session, child.id)
    return children


def roman(number: int) -> str:
    result = ""
    for value, symbol in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                          (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                          (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        count, number = divmod(number, value)
        result += symbol * count
    return result


def create_subtask(session: Session, parent: Task, title: str) -> Task:
    title = title.strip()
    if not title:
        raise ValueError("Subtask title is required")
    children = tasks_store.children(session, parent.id)
    child = tasks_store.create(session, TaskCreate(
        title=title[:512], description=parent.description, link=parent.link,
        due_date=parent.due_date, estimation=30, location=parent.location,
        label=parent.label, parent_task_id=parent.id, due_date_derived=True,
        related_event_id=parent.related_event_id,
        related_event_calendar_id=parent.related_event_calendar_id,
        related_event_due_derived=False,
        subtask_order=len(children),
    ))
    session.add(RawInput(
        source="subtask", content=title, status="open", task_id=child.id,
        source_metadata={"parent_task_id": str(parent.id), "parent_title": parent.title},
        processed_at=datetime.now(timezone.utc),
    ))
    parent.is_container = True
    if not children:
        parent.scheduled_date = None
    reflow_children(session, parent, [*children, child])
    return child


def split_deterministically(session: Session, task: Task) -> list[Task]:
    if task.parent_task_id or task.is_container:
        raise ValueError("Only a standalone task can be split")
    duration = task.estimation or 0
    if duration <= 0:
        raise ValueError("Set a duration before splitting")
    count = max(2, math.ceil(duration / 60))
    quotient, remainder = divmod(duration, count)
    children = []
    for index in range(count):
        estimate = quotient + (1 if index < remainder else 0)
        suffix = f" {roman(index + 1)}"
        child = tasks_store.create(session, TaskCreate(
            title=f"{task.title[:512 - len(suffix)]}{suffix}",
            description=task.description, link=task.link, location=task.location,
            label=task.label, due_date=task.due_date, estimation=estimate,
            parent_task_id=task.id, due_date_derived=True,
            related_event_id=task.related_event_id,
            related_event_calendar_id=task.related_event_calendar_id,
            related_event_due_derived=False,
            subtask_order=index,
        ))
        session.add(RawInput(
            source="subtask", content=child.title, status="open", task_id=child.id,
            source_metadata={"parent_task_id": str(task.id), "parent_title": task.title},
            processed_at=datetime.now(timezone.utc),
        ))
        children.append(child)
    task.is_container = True
    task.scheduled_date = None
    reflow_children(session, task, children)
    return children


async def split_task(
    session: Session, task: Task, *, force: bool = False
) -> list[Task]:
    """Create linked children once; external calendar work follows the DB commit."""
    existing = tasks_store.children(session, task.id)
    if existing:
        return existing
    if getattr(task, "parent_task_id", None) or (not force and not needs_split(task)):
        return []
    plans = await propose_subtasks(task)
    if not plans:
        return []
    deadlines = _deadlines(task.due_date, plans)
    children: list[Task] = []
    for index, plan in enumerate(plans):
        child = tasks_store.create(session, TaskCreate(
            title=plan.title,
            description=plan.description,
            estimation=plan.estimation,
            due_date=deadlines[index],
            location=task.location,
            link=task.link,
            label=task.label,
            parent_task_id=task.id,
            depends_on_task_id=children[plan.depends_on].id if plan.depends_on is not None else None,
            due_date_derived=plan.due_date is None,
            subtask_order=index,
        ))
        children.append(child)
        session.add(RawInput(
            source="subtask", content=plan.title,
            source_metadata={"parent_task_id": str(task.id), "parent_title": task.title},
            status="open", task_id=child.id,
            processed_at=datetime.now(timezone.utc),
        ))
    task.is_container = True
    task.due_date = children[-1].due_date
    task.estimation = sum(getattr(child, "estimation", None) or 30 for child in children)
    session.commit()
    if task.calendar_event_id:
        await delete_task_event(session, task)
    task.scheduled_date = None
    session.commit()
    publish_task(session, task.id)
    for child in children:
        publish_task(session, child.id)
        try:
            await schedule_task(session, child)
        except Exception:  # noqa: BLE001 — keep the saved split on calendar failure
            log.exception("subtask scheduling failed · task=%s", child.id)
    return children


async def maybe_split_task(session: Session, task: Task) -> list[Task]:
    if not needs_split(task) or getattr(task, "parent_task_id", None) or getattr(task, "is_container", False):
        return []
    try:
        return await split_task(session, task)
    except Exception:  # noqa: BLE001 — original task remains actionable
        log.exception("automatic task split failed · task=%s", task.id)
        return []
