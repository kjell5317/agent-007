import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Feedback(Base):
    """User-supplied feedback on an agent decision.

    Captures both "this task was wrong" corrections and "this was a duplicate"
    signals. Becomes the knowledge base for few-shot examples / future tuning.
    """

    __tablename__ = "feedback"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True, index=True
    )
    raw_input_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_inputs.id"), nullable=True, index=True
    )

    # TODO: enum (correct | wrong_fields | not_a_task | duplicate_of | merged | other)
    kind: Mapped[str] = mapped_column(String(32), index=True)

    # Free-text user note plus a structured patch describing the correction.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
