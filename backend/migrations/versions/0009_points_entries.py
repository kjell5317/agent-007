"""points_entries: ledger of points-earning events

Revision ID: 0009_points_entries
Revises: 0008_route_cache
Create Date: 2026-06-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_points_entries"
down_revision = "0008_route_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "points_entries",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("section", sa.String(32), nullable=True),
        sa.Column("action_name", sa.String(128), nullable=True),
        sa.Column(
            "task_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("factor", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_points_entries_task_id", "points_entries", ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_points_entries_task_id", table_name="points_entries")
    op.drop_table("points_entries")
