import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBEDDING_DIM = 1536


class ChatAnswer(Base):
    """A cached (question → answer) pair from a completed, read-only chat turn.
    The question is embedded so a later, similarly-phrased question can pull the
    prior answer into context as a HINT the model reuses or re-derives — never a
    short-circuit, since the underlying tasks/calendar/mail keep moving. Only
    clean turns are cached: no error/too-many-steps answers, and no turns that
    changed state (those are actions, not reusable facts)."""

    __tablename__ = "chat_answers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
