import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PointsEntry(Base):
    """One points event — a manual adjustment, completed task, or penalty.

    The current total is `SUM(amount)`. `task_id` is set for task-tied events
    (no FK — the task row can be deleted on close/dismiss) and lets callers
    enforce idempotency for awards and penalties.
    """

    __tablename__ = "points_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source: Mapped[str] = mapped_column(String(16))  # "manual" | "task" | "penalty"
    section: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    period_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    factor: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
