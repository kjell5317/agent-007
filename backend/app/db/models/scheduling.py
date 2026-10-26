"""Training data and fitted parameters for preference-based scheduling."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

ScheduleLogKind = (
    "placed",
    "moved_by_user",
    "resized_by_user",
    "checkin_sent",
    "still_working",
    "not_done",
    "done_confirmed",
    "closed",
    "no_response",
    "abandoned",
    "deferred",
    "unplaced",
    "rescheduled",
)


class ScheduleLog(Base):
    """One scheduling-relevant event. `features` is a snapshot taken at that
    moment: neighbours change later, so it can't be recomputed afterwards."""

    __tablename__ = "schedule_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(16))  # planner | user | checkin | system
    slot_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prev_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prev_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    solver_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskOverride(Base):
    """A user correction of a value the agent chose."""

    __tablename__ = "task_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    raw_input_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_inputs.id", ondelete="SET NULL"), nullable=True
    )
    # due_date | deadline_kind | estimation | label | work_type | effort | is_task
    field: Mapped[str] = mapped_column(String(24), index=True)
    agent_value: Mapped[object | None] = mapped_column(JSON, nullable=True)
    user_value: Mapped[object | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16))  # ui | calendar | notification | backfill
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LearnedModel(Base):
    """A versioned set of fitted parameters; one active row per kind."""

    __tablename__ = "learned_models"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSON)
    n_samples: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    fitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommuteFlip(Base):
    """A commute leg whose mode the user flipped through the event's link.
    Future legs keep the flipped mode as a pin until they're past; every row
    is a pairwise training example (mode taken vs mode planned)."""

    __tablename__ = "commute_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    origin_anchor: Mapped[str] = mapped_column(String(256))
    dest_anchor: Mapped[str] = mapped_column(String(256))
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    destination: Mapped[str | None] = mapped_column(Text, nullable=True)
    depart: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    arrive: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_mode: Mapped[str] = mapped_column(String(16))
    flipped_mode: Mapped[str] = mapped_column(String(16))
    # The leg had already started when it was flipped: a correction of what
    # happened (moves the derived bike location), not a pin.
    past: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # {"planned": φ(planned mode), "flipped": φ(flipped mode)}
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CommuteStateRow(Base):
    """Single row: the bike's and the user's position after the last
    finished commute leg (None location = home). `applied_through` is that
    leg's departure, so a leg is never folded in twice."""

    __tablename__ = "commute_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    bike_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    bike_anchor: Mapped[str] = mapped_column(String(256), default="home", server_default="home")
    bike_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_anchor: Mapped[str] = mapped_column(String(256), default="home", server_default="home")
    user_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
