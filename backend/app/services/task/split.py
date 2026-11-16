"""Turn a broad task into linked, schedulable pieces."""

from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.agent.helpers.llm import chat, user_message
from app.agent.triage.hints import split_hint as jev_split_hint
from app.config import get_settings
from app.db.clients import handoffs as handoffs_store
from app.db.clients import tasks as tasks_store
from app.db.models.raw_input import RawInput
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate
from app.events import publish_task, publish_task_removed
from app.services.calendar import delete_task_event
from app.services.notify import clear_task_notification
from app.services.plan import schedule_task
from app.services.plan.replan import batch as replan_batch

log = logging.getLogger(__name__)

_CONJUNCTION = re.compile(
    r"(?iu)(?<!\w)(?:and|und|et|y|e|en|och|og|ja|i|a|ve|és|și|si|&|\+)(?!\w)|[と及和与＆]"
)
# Gemini's thinking spends the output budget too; at the 1024 default it
# truncates the plan before the tool call is complete.
_MAX_TOKENS = 4096
# Models quote Markdown descriptions without their emphasis and code markers.
_MARKUP = re.compile(r"[*_`~]+")
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
                        "after_previous": {"type": "boolean", "description": "True when this step can start only after the step listed before it is done."},
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
    after_previous: bool = False
    due_date: datetime | None = None


def _evidence_text(text: str) -> str:
    return " ".join(_MARKUP.sub("", text).split()).casefold()


def needs_split(task: Task, *, split_hint: bool | None = None) -> bool:
    """Estimate is minutes. Jev's `split_hint`, when given, replaces the
    conjunction match on the title."""
    if (getattr(task, "estimation", None) or 0) > 120:
        return True
    if split_hint is not None:
        return split_hint
    return bool(_CONJUNCTION.search(getattr(task, "title", "") or ""))


async def propose_subtasks(task: Task) -> list[SubtaskPlan]:
    settings = get_settings()
    gemini = bool(settings.gemini_api_key)
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
            + "List the steps in the order they will be done. Use the overall "
            "deadline. Set after_previous when a step can start only after the "
            "step listed before it is done; leave it false for steps that can "
            "run in parallel. Set a step's "
            "due_date only if the source explicitly gives that step a separate "
            "deadline. Do not invent event dates. Call propose_subtasks."
        ),
        tools=[_PLAN_TOOL],
        force_tool="propose_subtasks",
        provider="google" if gemini else None,
        model=settings.chat_llm_model if gemini else None,
        thinking_level=settings.chat_thinking_level if gemini else None,
        max_tokens=_MAX_TOKENS,
        name="split-task-plan",
    )
    call = next((c for c in response.tool_calls if c.name == "propose_subtasks"), None)
    if call is None:
        if response.stop_reason in ("length", "max_tokens"):
            raise ValueError("Subtask planner hit its output token limit")
        raise ValueError("Subtask planner returned no plan")
    raw = call.input.get("subtasks")
    if not isinstance(raw, list) or (len(raw) != 0 and not 2 <= len(raw) <= 6):
        raise ValueError("Subtask planner must return 0 or 2–6 steps")
    plans: list[SubtaskPlan] = []
    source_text = _evidence_text(f"{task.title}\n{task.description or ''}")
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("Invalid subtask plan")
        title = str(item.get("title") or "").strip()[:512]
        evidence = _evidence_text(str(item.get("source_excerpt") or ""))
        estimate = int(item.get("estimation") or 0)
        if not title or not 5 <= estimate <= 120:
            raise ValueError("Subtask title or estimate is invalid")
        if not evidence or evidence not in source_text:
            raise ValueError("Subtask lacks source evidence")
        due = None
        if item.get("due_date"):
            due = datetime.fromisoformat(str(item["due_date"]).replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        plans.append(SubtaskPlan(
            title=title,
            description=str(item.get("description") or "").strip() or None,
            estimation=estimate,
            after_previous=index > 0 and item.get("after_previous") is True,
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


@dataclass
class RemovedMirror:
    """What `delete_task_event` needs of a deleted subtask: its calendar event
    and slot (the slot locates its commute legs)."""

    id: uuid.UUID
    calendar_event_id: str | None
    scheduled_date: datetime | None


def collapse_subtasks(session: Session, parent: Task) -> list[RemovedMirror]:
    """Undo a split in the database: delete the subtasks and make the parent a
    normal task again, carrying the work still open. Their calendar events go
    in `finish_collapse`, after the response."""
    if not parent.is_container:
        raise ValueError("Task has no subtasks")
    if tasks_store.latest_status_for(session, [parent.id]).get(parent.id, "open") != "open":
        raise ValueError("Only an open task can be collapsed")
    children = tasks_store.children(session, parent.id)
    statuses = tasks_store.latest_status_for(session, [child.id for child in children])
    remaining = sum(
        child.estimation or 30 for child in children if statuses.get(child.id, "open") == "open"
    )
    removed = [
        RemovedMirror(child.id, child.calendar_event_id, child.scheduled_date) for child in children
    ]
    for child in children:
        # Subtask rows and their generated `subtask` inputs carry no history.
        tasks_store.delete_with_inputs(session, child)
    parent.is_container = False
    parent.estimation = remaining or parent.estimation
    session.commit()
    for mirror in removed:
        publish_task_removed(mirror.id)
    publish_task(session, parent.id)
    return removed


async def finish_collapse(session: Session, parent_id: uuid.UUID, removed: list[RemovedMirror]) -> None:
    """The calendar side: drop the subtasks' events and commute legs, then
    place the parent; that solve also rewrites the commute legs."""
    for mirror in removed:
        await delete_task_event(session, mirror)
        await clear_task_notification(mirror.id)
    parent = tasks_store.get(session, parent_id)
    if parent is not None:
        await schedule_task(session, parent)
        publish_task(session, parent_id)


def roman(number: int) -> str:
    result = ""
    for value, symbol in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                          (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                          (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        count, number = divmod(number, value)
        result += symbol * count
    return result


def inherited_attributes(parent: Task) -> dict:
    """Subtasks share the parent's work type and effort, and its deadline
    kind: their derived deadlines exist to meet the parent's."""
    return {
        "work_type": getattr(parent, "work_type", None),
        "effort": getattr(parent, "effort", None),
        "deadline_kind": getattr(parent, "deadline_kind", None) or "firm",
    }


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
        **inherited_attributes(parent),
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
    # Whole quarter hours per part: durations live on the 15-minute grid.
    quarters = -(-duration // 15)
    if quarters < 2:
        raise ValueError("A 15-minute task can't be split")
    count = min(quarters, max(2, math.ceil(duration / 60)))
    quotient, remainder = divmod(quarters, count)
    children = []
    for index in range(count):
        estimate = 15 * (quotient + (1 if index < remainder else 0))
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
            **inherited_attributes(task),
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
            depends_on_task_id=children[-1].id if plan.after_previous else None,
            due_date_derived=plan.due_date is None,
            subtask_order=index,
            **inherited_attributes(task),
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
    await place_subtasks(session, task, children)
    return children


async def place_subtasks(session: Session, parent: Task, children: list[Task]) -> None:
    """Calendar side of a split: the parent's event goes, and every subtask is
    placed in one solve (each solve can take the solver's whole time limit)."""
    if parent.calendar_event_id:
        await delete_task_event(session, parent)
    parent.scheduled_date = None
    session.commit()
    publish_task(session, parent.id)
    async with replan_batch(session):
        for child in children:
            publish_task(session, child.id)
            try:
                await schedule_task(session, child)
            except Exception:  # noqa: BLE001 — keep the saved split on calendar failure
                log.exception("subtask scheduling failed · task=%s", child.id)


async def maybe_split_task(
    session: Session, task: Task, *, split_hint: bool | None = None
) -> list[Task]:
    if getattr(task, "parent_task_id", None) or getattr(task, "is_container", False):
        return []
    # Splitting would turn the task into a parent, where the agent's result
    # isn't shown. Only the user's explicit split still applies.
    if handoffs_store.latest_for_task(session, task.id) is not None:
        return []
    long_task = (getattr(task, "estimation", None) or 0) > 120
    conjunction = _CONJUNCTION.search(getattr(task, "title", "") or "")
    if split_hint is None and not long_task and conjunction:
        # A conjunction is only a cue ("Tom and Jerry"); Jev confirms it when acting.
        split_hint = await jev_split_hint(task)
    if not needs_split(task, split_hint=split_hint):
        return []
    try:
        return await split_task(session, task)
    except Exception:  # noqa: BLE001 — original task remains actionable
        log.exception("automatic task split failed · task=%s", task.id)
        return []
