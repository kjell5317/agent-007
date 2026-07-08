import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Computed, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.db.models.task import Task

EMBEDDING_DIM = 1536

# Single source of truth for the lifecycle enum. Mirrored by a CHECK constraint
# in 0003_reshape — keep in sync.
RawInputStatus = (
    "processing",  # in flight
    "not_task",    # not actionable
    "duplicate",   # matched an existing task (see task_id)
    "open",        # produced a task that is still open (see task_id)
    "closed",      # produced a task that is now done (see task_id)
    "event",       # created or updated a calendar event, without a task
)


class RawInput(Base):
    """Normalized envelope around any incoming message.

    Carries the pipeline's *decision* via `status` and the link to the task
    (if any) it produced or attached to. Multiple raw_inputs can share one
    `task_id` (Gmail thread follow-ups, manual updates) — the latest row
    represents the task's current state.
    """

    __tablename__ = "raw_inputs"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_raw_inputs_source_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    content: Mapped[str] = mapped_column(Text)
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="processing", index=True)

    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    task: Mapped[Task | None] = relationship(Task, lazy="joined")

    agent_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    # Keyword side of suggest + the hybrid precedent lookup (subject + sender +
    # channel + content), generated in the DB; read-only here.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', "
            "coalesce(source_metadata->>'subject', '') || ' ' || "
            "coalesce(source_metadata->>'from', '') || ' ' || "
            "coalesce(source_metadata->>'channel_name', '') || ' ' || "
            "coalesce(content, ''))",
            persisted=True,
        ),
        nullable=True,
    )
