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
    agent extraction over the raw input. User-provided values always
    override agent guesses."""

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
    captures the whole conversation. The path's raw_input is the anchor."""

    context_input_ids: list[uuid.UUID] = Field(default_factory=list)


class TaskCreationAccepted(BaseModel):
    """Response for POST /tasks — the raw_input has landed, agent work is
    queued. Clients poll GET /inputs/{raw_input_id} until status moves off
    'processing' to know when the task itself is ready."""

    raw_input_id: uuid.UUID
    status: str = "processing"


class TaskRead(TaskBase):
    id: uuid.UUID
    scheduled_date: datetime | None = None
    status: str  # derived from latest linked raw_input
    is_manual: bool  # true if every linked raw_input has source='manual'
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def build(cls, task, status_: str, is_manual: bool) -> "TaskRead":
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
                "estimation": task.estimation,
                "location": task.location,
                "label": task.label,
                "status": status_,
                "is_manual": is_manual,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
        )
