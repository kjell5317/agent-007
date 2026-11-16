import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TaskHandoff(Base):
    """Work an agent does on a task after it's created: research, an email
    draft, or a kotx code-change run. At most one is `proposed` or `running`
    per task."""

    __tablename__ = "task_handoffs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    brief: Mapped[str] = mapped_column(Text)
    # proposed | running | ready | failed | declined
    state: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The agent's tool calls, shaped like a raw input's `agent_trace`.
    trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
