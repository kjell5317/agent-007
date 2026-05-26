"""tasks: add label

Revision ID: 0005_task_label
Revises: 0004_task_calendar_event_id
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_task_label"
down_revision = "0004_task_calendar_event_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("label", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "label")
