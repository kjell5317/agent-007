"""notes: agent's long-term memory carved out of mark_not_task inputs

Revision ID: 0007_notes_table
Revises: 0006_task_ai_doable
Create Date: 2026-05-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0007_notes_table"
down_revision = "0006_task_ai_doable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "source_raw_input_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_inputs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_notes_source_raw_input_id",
        "notes",
        ["source_raw_input_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notes_source_raw_input_id", table_name="notes")
    op.drop_table("notes")
