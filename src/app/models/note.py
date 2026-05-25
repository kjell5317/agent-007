import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBEDDING_DIM = 1536


class Note(Base):
    """A standalone fact extracted from a raw input the agent did NOT turn
    into a task. Notes are the agent's long-term memory — when deciding
    about a future input it can call `search_notes(query)` to retrieve
    relevant ones."""

    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    content: Mapped[str] = mapped_column(Text)

    source_raw_input_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_inputs.id", ondelete="SET NULL"),
        nullable=True,
    )

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
