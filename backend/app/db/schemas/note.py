import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.db.clients.notes import NoteListItem


class NoteUpdate(BaseModel):
    content: str


class NoteRead(BaseModel):
    id: uuid.UUID
    content: str
    source: str | None
    source_from: str | None
    source_subject: str | None
    source_raw_input_id: uuid.UUID | None
    created_at: datetime

    @classmethod
    def from_item(cls, item: "NoteListItem") -> "NoteRead":
        return cls(
            id=item.id,
            content=item.content,
            source=item.source,
            source_from=item.source_from,
            source_subject=item.source_subject,
            source_raw_input_id=item.source_raw_input_id,
            created_at=item.created_at,
        )
