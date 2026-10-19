"""Preference-based scheduling: task attributes, schedule log, overrides,
learned models."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_scheduling_learning"
down_revision = "0037_task_recurrence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("work_type", sa.String(16), nullable=True))
    op.add_column("tasks", sa.Column("effort", sa.String(8), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("deadline_kind", sa.String(8), nullable=False, server_default="firm"),
    )
    op.add_column(
        "tasks",
        sa.Column("placed_by", sa.String(16), nullable=False, server_default="planner"),
    )
    op.add_column("tasks", sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tasks", sa.Column("placed_context", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "tasks", sa.Column("running_since", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("tasks", sa.Column("booked_minutes", sa.Integer(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("schedule_state", sa.String(16), nullable=False, server_default="placed"),
    )
    op.execute("UPDATE tasks SET schedule_state = 'unplaced' WHERE scheduled_date IS NULL")
    op.execute("UPDATE tasks SET deadline_kind = 'soft' WHERE due_date_derived")

    op.add_column("labels", sa.Column("default_work_type", sa.String(16), nullable=True))
    op.add_column("labels", sa.Column("default_effort", sa.String(8), nullable=True))

    op.create_table(
        "schedule_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("slot_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("slot_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prev_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prev_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("solver_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_schedule_log_task_id", "schedule_log", ["task_id"])
    op.create_index("ix_schedule_log_kind_created", "schedule_log", ["kind", "created_at"])

    op.create_table(
        "task_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "raw_input_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_inputs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("field", sa.String(24), nullable=False),
        sa.Column("agent_value", postgresql.JSONB(), nullable=True),
        sa.Column("user_value", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_task_overrides_task_id", "task_overrides", ["task_id"])
    op.create_index("ix_task_overrides_field", "task_overrides", ["field"])

    op.create_table(
        "learned_models",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("n_samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "fitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_learned_models_kind_active", "learned_models", ["kind", "active"])
    op.create_index(
        "uq_learned_models_one_active",
        "learned_models",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index("uq_learned_models_one_active", table_name="learned_models")
    op.drop_index("ix_learned_models_kind_active", table_name="learned_models")
    op.drop_table("learned_models")
    op.drop_index("ix_task_overrides_field", table_name="task_overrides")
    op.drop_index("ix_task_overrides_task_id", table_name="task_overrides")
    op.drop_table("task_overrides")
    op.drop_index("ix_schedule_log_kind_created", table_name="schedule_log")
    op.drop_index("ix_schedule_log_task_id", table_name="schedule_log")
    op.drop_table("schedule_log")
    op.drop_column("labels", "default_effort")
    op.drop_column("labels", "default_work_type")
    for column in (
        "schedule_state", "booked_minutes", "running_since", "placed_context", "placed_at",
        "placed_by", "deadline_kind", "effort", "work_type",
    ):
        op.drop_column("tasks", column)
