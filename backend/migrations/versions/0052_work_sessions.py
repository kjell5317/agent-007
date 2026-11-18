"""Work sessions (start / pause / complete, logged work), the time a task has
been worked on and waited on, and last night's sleep per day."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_work_sessions"
down_revision = "0051_notification_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(16), nullable=True),
        sa.Column("source", sa.String(8), nullable=False, server_default="live"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_work_sessions_task_id", "work_sessions", ["task_id"])
    op.create_index("ix_work_sessions_started_at", "work_sessions", ["started_at"])
    op.add_column("tasks", sa.Column("worked_minutes", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("waiting_since", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "day_context",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("sleep_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wake_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sleep_minutes", sa.Integer(), nullable=True),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("day_context")
    op.drop_column("tasks", "waiting_since")
    op.drop_column("tasks", "worked_minutes")
    op.drop_index("ix_work_sessions_started_at", table_name="work_sessions")
    op.drop_index("ix_work_sessions_task_id", table_name="work_sessions")
    op.drop_table("work_sessions")
