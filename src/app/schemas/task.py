import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TaskBase(BaseModel):
    title: str
    description: str | None = None
    estimated_minutes: int | None = None
    location: str | None = None
    due_at: datetime | None = None
    source_links: list[str] = Field(default_factory=list)


class TaskCreate(TaskBase):
    """Used both by the agent (after extraction) and by the manual create endpoint."""

    confidence: float | None = None
    raw_input_id: uuid.UUID | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    estimated_minutes: int | None = None
    location: str | None = None
    due_at: datetime | None = None
    source_links: list[str] | None = None
    status: str | None = None


class TaskRead(TaskBase):
    id: uuid.UUID
    confidence: float | None
    status: str
    raw_input_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
