import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Task(Base):
    """An actionable task. Lifecycle state lives on its linked raw_inputs."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Also required: a task without a deadline can't be placed in the planner's
    # `[now, due]` window. `create()` fills a horizon default when the agent
    # can't extract one; server_default is the NOT NULL safety net.
    due_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Every task carries a slot. New rows default to now() so the invariant
    # holds before the planner assigns a real slot; a task that never lands one
    # keeps a past slot that the overdue-reschedule cron keeps re-planning.
    scheduled_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    estimation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Free-form label name; the catalog + per-label color live in
    # `config/labels.toml`. Nullable: legacy rows and tasks created before
    # the labels config was populated may have no label.
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
