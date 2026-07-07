import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBEDDING_DIM = 1536


class Note(Base):
    """A standalone fact an agent flow extracted from a raw input alongside
    its terminal decision (create/update/no_change/mark_not_task). Notes are
    the agent's long-term memory — when deciding about a future input it can
    call `search_notes(query)` to retrieve relevant ones."""

    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    content: Mapped[str] = mapped_column(Text)

    source_raw_input_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_inputs.id", ondelete="SET NULL"),
        nullable=True,
    )

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    # Generated in the DB (keyword side of the hybrid lookup); read-only here.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(content, ''))", persisted=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
