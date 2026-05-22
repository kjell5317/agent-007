"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "raw_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False, index=True),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("source_metadata", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="received", index=True),
        sa.Column("agent_trace", sa.JSON, nullable=True),
        sa.UniqueConstraint("source", "external_id", name="uq_raw_inputs_source_external_id"),
    )

    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("estimated_minutes", sa.Integer, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("location", sa.String(256), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_links", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("status", sa.String(32), nullable=False, server_default="open", index=True),
        sa.Column(
            "raw_input_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_inputs.id"),
            nullable=True,
        ),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tasks_raw_input_id", "tasks", ["raw_input_id"])

    op.create_table(
        "feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column(
            "raw_input_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_inputs.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("correction", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_feedback_task_id", "feedback", ["task_id"])
    op.create_index("ix_feedback_raw_input_id", "feedback", ["raw_input_id"])

    op.create_table(
        "oauth_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False, index=True),
        sa.Column("account_key", sa.String(256), nullable=False),
        sa.Column("access_token_ct", sa.LargeBinary, nullable=False),
        sa.Column("refresh_token_ct", sa.LargeBinary, nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("extra", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "account_key", name="uq_oauth_tokens_provider_account"),
    )


def downgrade() -> None:
    op.drop_table("oauth_tokens")
    op.drop_table("feedback")
    op.drop_table("tasks")
    op.drop_table("raw_inputs")
    op.execute("DROP EXTENSION IF EXISTS vector")
