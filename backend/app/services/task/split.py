"""Turn a broad task into linked, schedulable pieces."""

from __future__ import annotations

import logging
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
    due = [min(parent_due, p.due_date) if p.due_date else parent_due for p in plans]
    for index in range(len(plans) - 1, -1, -1):
        predecessor = plans[index].depends_on
        if predecessor is not None:
            latest = due[index] - timedelta(minutes=plans[index].estimation + 15)
            due[predecessor] = min(due[predecessor], latest)
    return due


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
        ))
        children.append(child)
        session.add(RawInput(
            source="subtask", content=plan.title,
            source_metadata={"parent_task_id": str(task.id), "parent_title": task.title},
            status="open", task_id=child.id,
            processed_at=datetime.now(timezone.utc),
        ))
    task.is_container = True
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


async def refresh_child_deadlines(session: Session, parent: Task) -> None:
    children = tasks_store.children(session, parent.id)
    if not children:
        return
    index = {child.id: position for position, child in enumerate(children)}
    plans = [SubtaskPlan(
        title=child.title, description=child.description,
        estimation=child.estimation or 30,
        depends_on=index.get(child.depends_on_task_id),
        due_date=None if child.due_date_derived else child.due_date,
    ) for child in children]
    deadlines = _deadlines(parent.due_date, plans)
    from app.services.task.update import update_task
    for child, due in zip(children, deadlines):
        if child.due_date_derived and child.due_date != due:
            await update_task(session, child.id, {"due_date": due, "due_date_derived": True})
