import uuid
from datetime import datetime

from pydantic import BaseModel


class FeedbackCreate(BaseModel):
    task_id: uuid.UUID | None = None
    raw_input_id: uuid.UUID | None = None
    kind: str  # TODO: replace with Literal/Enum once kinds stabilize
    note: str | None = None
    correction: dict | None = None


class FeedbackRead(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID | None
    raw_input_id: uuid.UUID | None
    kind: str
    note: str | None
    correction: dict | None
    created_at: datetime

    class Config:
        from_attributes = True
