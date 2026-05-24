"""add embedding column to raw_inputs

Revision ID: 0002_raw_input_embedding
Revises: 0001_initial
Create Date: 2026-05-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002_raw_input_embedding"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_inputs", sa.Column("embedding", Vector(1536), nullable=True))


def downgrade() -> None:
    op.drop_column("raw_inputs", "embedding")
