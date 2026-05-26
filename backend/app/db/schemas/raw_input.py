import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.db.models.raw_input import RawInput


class RawInputCreate(BaseModel):
    """Generic envelope for any incoming message."""

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
    task_id: uuid.UUID | None
    task_title: str | None
    agent_trace: dict | None

    @classmethod
    def from_row(cls, row: "RawInput") -> "RawInputRead":
        return cls(
            id=row.id,
            source=row.source,
            external_id=row.external_id,
            content=row.content,
            source_metadata=row.source_metadata,
            received_at=row.received_at,
            processed_at=row.processed_at,
            status=row.status,
            task_id=row.task_id,
            task_title=row.task.title if row.task is not None else None,
            agent_trace=row.agent_trace,
        )
