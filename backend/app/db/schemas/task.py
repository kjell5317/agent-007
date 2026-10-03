from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TaskScheduleStatus = Literal["scheduled", "pending", "unscheduled"]


class SubtaskSummary(BaseModel):
    id: uuid.UUID
    public_id: str | None = None
    title: str
    due_date: datetime
    estimation: int | None
    status: str
    depends_on_task_id: uuid.UUID | None = None


class TaskBase(BaseModel):
    title: str
    description: str | None = None
    link: str | None = None
    due_date: datetime | None = None
    estimation: int | None = None
    location: str | None = None
    label: str | None = None
    related_event_id: str | None = None
    related_event_calendar_id: str | None = None
    related_event_due_derived: bool = False
    parent_task_id: uuid.UUID | None = None
    depends_on_task_id: uuid.UUID | None = None
    is_container: bool = False
    due_date_derived: bool = False
    subtask_order: int | None = None


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    link: str | None = None
    due_date: datetime | None = None
    estimation: int | None = None
    location: str | None = None
    label: str | None = None
    related_event_id: str | None = None
    related_event_calendar_id: str | None = None
    related_event_due_derived: bool | None = None


class TaskPromote(BaseModel):
    """Body for POST /tasks/open/{raw_input_id} and POST /tasks. Every field
    optional — anything missing from title/estimation/due_date triggers an
    agent decision to create or update a task from the raw input. `content` is source text for manual
    composer submissions, not a task field override. User-provided structured
    values always override agent guesses."""

    content: str | None = None
    title: str | None = None
    description: str | None = None
    link: str | None = None
    due_date: datetime | None = None
    estimation: int | None = None
    location: str | None = None
    label: str | None = None
    related_event_id: str | None = None
    related_event_calendar_id: str | None = None


class TaskOpenRequest(TaskPromote):
    """Body for POST /tasks/open/{raw_input_id}. Adds `context_input_ids` —
    sibling inputs from the same thread/follow-up group whose content should
    also feed the agent's extraction, so a task created from a grouped thread
    captures the whole conversation. `target_task_id` lets callers name an
    existing task for follow-up handling; it is available for API clients but
    not yet wired into the frontend. The path's raw_input is the anchor."""

    context_input_ids: list[uuid.UUID] = Field(default_factory=list)
    target_task_id: uuid.UUID | None = None


class TaskCreationAccepted(BaseModel):
    """Response for POST /tasks — the raw_input has landed, agent work is
    queued. Clients poll GET /inputs/{raw_input_id} until status moves off
    'processing' to know when the task itself is ready."""

    raw_input_id: uuid.UUID
    status: str = "processing"


class LocationSuggestionsRead(BaseModel):
    suggestions: list[str]


class TaskRawInputRead(BaseModel):
    id: uuid.UUID
    source: str
    external_id: str | None
    content: str
    source_metadata: dict
    received_at: datetime
    processed_at: datetime | None
    status: str
    task_id: uuid.UUID | None
    task_title: str | None
    agent_trace: dict | None
    source_url: str | None

    @classmethod
    def build(cls, raw_input, source_url: str | None = None) -> "TaskRawInputRead":
        return cls(
            id=raw_input.id,
            source=raw_input.source,
            external_id=raw_input.external_id,
            content=raw_input.content,
            source_metadata=raw_input.source_metadata,
            received_at=raw_input.received_at,
            processed_at=raw_input.processed_at,
            status=raw_input.status,
            task_id=raw_input.task_id,
            task_title=raw_input.task.title if raw_input.task is not None else None,
            agent_trace=raw_input.agent_trace,
            source_url=source_url,
        )


class TaskRead(TaskBase):
    id: uuid.UUID
    public_id: str | None = None
    parent_card: TaskRead | None = None
    scheduled_date: datetime | None = None
    schedule_status: TaskScheduleStatus
    source_url: str | None = None
    raw_inputs: list[TaskRawInputRead] = Field(default_factory=list)
    subtasks: list[SubtaskSummary] = Field(default_factory=list)
    status: str  # derived from latest linked raw_input
    is_manual: bool  # true if every linked raw_input has source='manual'
    kotx_task_id: int | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def build(
        cls,
        task,
        status_: str,
        is_manual: bool,
        source_url: str | None = None,
        raw_inputs: list[TaskRawInputRead] | None = None,
        subtasks: list[SubtaskSummary] | None = None,
        scheduling: str | None = None,
        parent_card: TaskRead | None = None,
    ) -> "TaskRead":
        """Assemble the read model from an ORM row plus its derived
        `status` / `is_manual` (both come from separate queries — see
        `tasks.latest_status_for` / `tasks.is_manual_for`)."""
        # An open task is "scheduled" only with BOTH a real slot
        # (`scheduled_date`) and a live calendar mirror. Missing either reads
        # as unscheduled (red): no slot yet, or a stale mirror id left behind
        # after the slot was cleared (the latter kept #166 grey).
        scheduled = task.scheduled_date is not None and bool(
            getattr(task, "calendar_event_id", None)
        )
        schedule_status: TaskScheduleStatus = "scheduled"
        if status_ == "open" and not scheduled:
            latest = next((item for item in (raw_inputs or []) if item.status != "duplicate"), None)
            scheduling = scheduling if scheduling is not None else (latest.agent_trace or {}).get("scheduling") if latest else None
            schedule_status = "pending" if scheduling in {"pending", "not_needed"} else "unscheduled"
        if getattr(task, "is_container", False):
            schedule_status = "pending"
        return cls.model_validate(
            {
                "id": task.id,
                "public_id": getattr(task, "public_id", None),
                "parent_card": parent_card,
                "title": task.title,
                "description": task.description,
                "link": task.link,
                "due_date": task.due_date,
                "scheduled_date": task.scheduled_date,
                "schedule_status": schedule_status,
                "source_url": source_url,
                "raw_inputs": raw_inputs or [],
                "subtasks": subtasks or [],
                "estimation": task.estimation,
                "location": task.location,
                "label": task.label,
                "related_event_id": getattr(task, "related_event_id", None),
                "related_event_calendar_id": getattr(task, "related_event_calendar_id", None),
                "related_event_due_derived": getattr(task, "related_event_due_derived", False),
                "parent_task_id": getattr(task, "parent_task_id", None),
                "depends_on_task_id": getattr(task, "depends_on_task_id", None),
                "is_container": getattr(task, "is_container", False),
                "due_date_derived": getattr(task, "due_date_derived", False),
                "subtask_order": getattr(task, "subtask_order", None),
                "status": status_,
                "is_manual": is_manual,
                "kotx_task_id": getattr(task, "kotx_task_id", None),
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
        )
