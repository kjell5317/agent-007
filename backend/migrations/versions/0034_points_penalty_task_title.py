"""Keep task titles with points entries after tasks are removed."""

import sqlalchemy as sa
from alembic import op

revision = "0034_points_penalty_task_title"
down_revision = "0033_search_result_clicks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("points_entries", sa.Column("task_title", sa.String(length=512), nullable=True))
    op.execute("""
        UPDATE points_entries AS entry
        SET task_title = task.title
        FROM tasks AS task
        WHERE entry.task_id = task.id
          AND entry.source = 'penalty'
    """)


def downgrade() -> None:
    op.drop_column("points_entries", "task_title")
