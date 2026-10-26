import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Task(Base):
    """An actionable task. Lifecycle state lives on its linked raw_inputs."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    public_id: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)

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
    # The agent's own estimate, before the learned duration correction set
    # `estimation` at creation. Null for durations the user gave.
    agent_estimation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Who produced `agent_estimation`: "jev" for Jev's duration buckets, null
    # for the LLM agent. The duration model learns each estimator's bias apart.
    estimation_source: Mapped[str | None] = mapped_column(String(8), nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Label name, matching a row in `labels` (mirrored from the Google Calendar
    # event labels). Not a foreign key: a label deleted in Google shouldn't take
    # the task's history with it. Nullable — legacy rows may have no label.
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    related_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    related_event_calendar_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    related_event_due_derived: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    depends_on_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_container: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    due_date_derived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    subtask_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Repeat rule, copied onto every instance of a series (see
    # app.services.task.recur). The anchor is the due date occurrences count
    # from; the series id groups the instances. Not a foreign key: no single
    # instance owns the series.
    recurrence_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)
    recurrence_interval: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence_anchor: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recurrence_series_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    # Link to the kotx coding-agent task driving this work, when there is one.
    # One 007 task per kotx task; transitions arrive via webhook/poll and are
    # matched back through this id (see app.agent.kotx).
    kotx_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)

    # Scheduling attributes (see docs/scheduling-plan.md). `work_type` /
    # `effort` / `deadline_kind` feed the slot-preference model; the `placed_*`
    # fields record who chose the current slot and what surrounded it then, so
    # the solver can keep a hand-placed slot until its surroundings change.
    work_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    effort: Mapped[str | None] = mapped_column(String(8), nullable=True)
    deadline_kind: Mapped[str] = mapped_column(String(8), default="firm", server_default="firm")
    placed_by: Mapped[str] = mapped_column(String(16), default="planner", server_default="planner")
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    placed_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Set while a "Still working" extension runs; the slot then stays fixed and
    # its calendar event grows in 15-minute steps until the task is closed.
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Length the current slot was booked with when it differs from the
    # estimate (the solver rounds slots up to its 15-minute grid). Null means
    # the estimate itself. Calendar sync compares event lengths against this,
    # so such a booking doesn't read as a user resize.
    booked_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # placed | deferred | unplaced — `scheduled_date` is null for the last two.
    schedule_state: Mapped[str] = mapped_column(
        String(16), default="placed", server_default="placed"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # NOTE: a generated `tsv` column (title + description + label) exists in the
    # DB for stage-1 suggest (migration 0023) but is deliberately NOT mapped
    # here: it's DB-only, referenced by raw SQL in app.db.clients.search. Mapping
    # it would either force it into ORM INSERTs (Postgres rejects writes to a
    # generated column) or break the sqlite-backed unit tests that create this
    # table (sqlite has no TSVECTOR / to_tsvector).
