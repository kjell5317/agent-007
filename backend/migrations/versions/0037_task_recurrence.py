"""Repeat rules on tasks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037_task_recurrence"
down_revision = "0036_task_public_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("recurrence_unit", sa.String(8), nullable=True))
    op.add_column("tasks", sa.Column("recurrence_interval", sa.Integer(), nullable=True))
    op.add_column(
        "tasks", sa.Column("recurrence_anchor", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tasks",
        sa.Column("recurrence_series_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_tasks_recurrence_series_id", "tasks", ["recurrence_series_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_recurrence_series_id", table_name="tasks")
    op.drop_column("tasks", "recurrence_series_id")
    op.drop_column("tasks", "recurrence_anchor")
    op.drop_column("tasks", "recurrence_interval")
    op.drop_column("tasks", "recurrence_unit")
