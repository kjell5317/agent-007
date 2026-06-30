"""tasks: add scheduled_date

Revision ID: 0012_task_scheduled_date
Revises: 0011_drop_task_ai_doable
Create Date: 2026-06-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_task_scheduled_date"
down_revision = "0011_drop_task_ai_doable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("scheduled_date", sa.DateTime(timezone=True), nullable=True),
    )
    # Historical calendar start times were not stored locally before this
    # column existed, so this migration intentionally leaves existing managed
    # calendar tasks NULL. The app falls back to due_date for display/order
    # until future calendar discovery syncs the event start.


def downgrade() -> None:
    op.drop_column("tasks", "scheduled_date")
