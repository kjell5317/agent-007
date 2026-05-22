import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# TODO: pick an embedding model and pin the dimension to match
EMBEDDING_DIM = 1536


class Task(Base):
    """A task extracted from one (or more) raw inputs."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # TODO: list of source URLs / references; consider a separate join table later
    source_links: Mapped[list[str]] = mapped_column(JSON, default=list)

    # TODO: status enum (open | done | dismissed | duplicate)
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)

    # Origin: which raw input produced this task (nullable for manual creates).
    raw_input_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_inputs.id"), nullable=True, index=True
    )

    # Used for semantic dedup / knowledge-base retrieval.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # TODO: tags, priority, project — add as the schema stabilizes
