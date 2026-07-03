"""geocode_cache: cached address → (lat, lon) lookups

Revision ID: 0016_geocode_cache
Revises: 0015_points_entry_period_key
Create Date: 2026-07-03
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_geocode_cache"
down_revision = "0015_points_entry_period_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "geocode_cache",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("address", sa.String(512), nullable=False),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lon", sa.Float, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("address", name="uq_geocode_cache_address"),
    )


def downgrade() -> None:
    op.drop_table("geocode_cache")
