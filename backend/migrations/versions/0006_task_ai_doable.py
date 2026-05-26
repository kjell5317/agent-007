"""tasks: add ai_doable

Revision ID: 0006_task_ai_doable
Revises: 0005_task_label
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_task_ai_doable"
down_revision = "0005_task_label"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("ai_doable", sa.String(8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "ai_doable")
