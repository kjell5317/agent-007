"""tasks: drop ai_doable

Revision ID: 0011_drop_task_ai_doable
Revises: 0010_raw_input_status_event
Create Date: 2026-06-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_drop_task_ai_doable"
down_revision = "0010_raw_input_status_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tasks", "ai_doable")


def downgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("ai_doable", sa.String(8), nullable=True),
    )
