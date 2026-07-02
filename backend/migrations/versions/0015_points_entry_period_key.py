"""points_entries: period key for idempotent penalties

Revision ID: 0015_points_entry_period_key
Revises: 0014_due_date_not_null
Create Date: 2026-07-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_points_entry_period_key"
down_revision = "0014_due_date_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "points_entries",
        sa.Column("period_key", sa.String(128), nullable=True),
    )
    op.create_index(
        "uq_points_penalty_period",
        "points_entries",
        ["source", "action_name", "task_id", "period_key"],
        unique=True,
        postgresql_where=sa.text(
            "source = 'penalty' AND task_id IS NOT NULL AND period_key IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_points_penalty_period", table_name="points_entries")
    op.drop_column("points_entries", "period_key")
