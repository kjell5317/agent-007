import uuid
from datetime import datetime

from pydantic import BaseModel


class TaskBase(BaseModel):
    title: str
    description: str | None = None
    link: str | None = None
    due_date: datetime | None = None
    estimation: int | None = None
    location: str | None = None
    label: str | None = None
    ai_doable: str | None = None  # "yes" / "no" / "unsure"


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
    ai_doable: str | None = None


class TaskPromote(BaseModel):
    """Body for POST /inputs/{id}/open_task. Every field optional — anything
    missing from title/estimation/due_date triggers an agent extraction over
    the raw input. User-provided values always override agent guesses."""

    title: str | None = None
    description: str | None = None
    link: str | None = None
    due_date: datetime | None = None
    estimation: int | None = None
    location: str | None = None
    label: str | None = None
    ai_doable: str | None = None


class TaskCreationAccepted(BaseModel):
    """Response for POST /tasks — the raw_input has landed, agent work is
    queued. Clients poll GET /inputs/{raw_input_id} until status moves off
    'processing' to know when the task itself is ready."""

    raw_input_id: uuid.UUID
    status: str = "processing"


class TaskRead(TaskBase):
    id: uuid.UUID
    status: str  # derived from latest linked raw_input
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
