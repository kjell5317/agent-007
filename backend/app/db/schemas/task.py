import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TaskBase(BaseModel):
    title: str
    description: str | None = None
    link: str | None = None
    due_date: datetime | None = None
    estimation: int | None = None
    location: str | None = None
    label: str | None = None


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


class TaskPromote(BaseModel):
    """Body for POST /tasks/open/{raw_input_id} and POST /tasks. Every field
    optional — anything missing from title/estimation/due_date triggers an
    agent extraction over the raw input. `content` is source text for manual
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
    scheduled_date: datetime | None = None
    source_url: str | None = None
    raw_inputs: list[TaskRawInputRead] = Field(default_factory=list)
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
    ) -> "TaskRead":
        """Assemble the read model from an ORM row plus its derived
        `status` / `is_manual` (both come from separate queries — see
        `tasks.latest_status_for` / `tasks.is_manual_for`)."""
        return cls.model_validate(
            {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "link": task.link,
                "due_date": task.due_date,
                "scheduled_date": task.scheduled_date,
                "source_url": source_url,
                "raw_inputs": raw_inputs or [],
                "estimation": task.estimation,
                "location": task.location,
                "label": task.label,
                "status": status_,
                "is_manual": is_manual,
                "kotx_task_id": getattr(task, "kotx_task_id", None),
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
        )
