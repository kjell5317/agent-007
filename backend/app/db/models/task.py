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
    # Null means unscheduled: no valid slot exists yet (couldn't be placed
    # before the due date, or its stale slot was cleared). `open_unscheduled_due`
    # + the retry cron sweep keep re-planning these. New rows default to now()
    # so a fresh task holds a provisional slot until the planner assigns a real one.
    scheduled_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )
    estimation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Free-form label name; the catalog + per-label color live in
    # `config/labels.toml`. Nullable: legacy rows and tasks created before
    # the labels config was populated may have no label.
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Link to the kotx coding-agent task driving this work, when there is one.
    # One 007 task per kotx task; transitions arrive via webhook/poll and are
    # matched back through this id (see app.agent.kotx).
    kotx_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
