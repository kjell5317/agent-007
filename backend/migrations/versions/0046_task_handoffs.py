"""Agent hand-offs: research, lookups, drafts and kotx runs started from a
new task."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_task_handoffs"
down_revision = "0045_calendar_recurring_horizon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_task_handoffs_task_id", "task_handoffs", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_handoffs_task_id", table_name="task_handoffs")
    op.drop_table("task_handoffs")
