"""Persist the order of subtasks."""

import sqlalchemy as sa
from alembic import op

revision = "0035_subtask_order"
down_revision = "0034_points_penalty_task_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("subtask_order", sa.Integer(), nullable=True))
    op.execute("""
        WITH ordered AS (
            SELECT id, row_number() OVER (
                PARTITION BY parent_task_id ORDER BY created_at, id
            ) - 1 AS position
            FROM tasks WHERE parent_task_id IS NOT NULL
        )
        UPDATE tasks SET subtask_order = ordered.position
        FROM ordered WHERE tasks.id = ordered.id
    """)


def downgrade() -> None:
    op.drop_column("tasks", "subtask_order")
