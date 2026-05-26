"""tasks: add calendar_event_id

Revision ID: 0004_task_calendar_event_id
Revises: 0003_reshape
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_task_calendar_event_id"
down_revision = "0003_reshape"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("calendar_event_id", sa.String(256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "calendar_event_id")
