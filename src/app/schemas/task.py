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


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    link: str | None = None
    due_date: datetime | None = None
    estimation: int | None = None
    location: str | None = None


class TaskRead(TaskBase):
    id: uuid.UUID
    status: str  # derived from latest linked raw_input
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
