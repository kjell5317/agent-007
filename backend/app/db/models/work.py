import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WorkSession(Base):
    """One stretch of work on a task: from a Start tap to a pause, a switch to
    another task or Complete — or a span logged afterwards (`source="retro"`).
    `ended_at` is null while it runs."""

    __tablename__ = "work_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # break | waiting | interrupted | switched | completed | logged | abandoned | dismissed
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # live | retro
    source: Mapped[str] = mapped_column(String(8), default="live", server_default="live")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def minutes(self) -> float:
        if self.ended_at is None:
            return 0.0
        return max(0.0, (self.ended_at - self.started_at).total_seconds() / 60)


class DayContext(Base):
    """Last night's sleep, recorded once per local day."""

    __tablename__ = "day_context"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    sleep_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sleep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
