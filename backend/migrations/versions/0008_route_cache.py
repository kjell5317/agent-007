"""route_cache: cached Google Maps distance lookups

Revision ID: 0008_route_cache
Revises: 0007_notes_table
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_route_cache"
down_revision = "0007_notes_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "route_cache",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("origin", sa.String(512), nullable=False),
        sa.Column("destination", sa.String(512), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("hour_bucket", sa.Integer, nullable=False),
        sa.Column("duration_seconds", sa.Integer, nullable=False),
        sa.Column("distance_meters", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "origin", "destination", "mode", "hour_bucket",
            name="uq_route_cache_lookup",
        ),
    )


def downgrade() -> None:
    op.drop_table("route_cache")
