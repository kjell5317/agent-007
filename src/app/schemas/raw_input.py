import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RawInputCreate(BaseModel):
    """Generic envelope for any incoming message.

    Sources adapt their payloads into this shape before handing off
    to the processing pipeline.
    """

    source: str = Field(..., description="Source identifier, e.g. 'gmail', 'slack', 'manual'")
    external_id: str | None = Field(
        None, description="Stable per-source id used for dedup (message id, thread id, ...)"
    )
    content: str
    source_metadata: dict = Field(default_factory=dict)


class RawInputRead(BaseModel):
    id: uuid.UUID
    source: str
    external_id: str | None
    content: str
    source_metadata: dict
    received_at: datetime
    processed_at: datetime | None
    status: str

    class Config:
        from_attributes = True
