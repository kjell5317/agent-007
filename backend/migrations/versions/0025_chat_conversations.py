"""chat_conversations: persist chat/"ask" conversations

The chat view lists recent conversations and reloads them. Store each whole —
message list (roles, content, citations, tool traces) in one JSONB blob, plus a
derived `title` and timestamps. Ordered by `updated_at` for the recent list.

Revision ID: 0025_chat_conversations
Revises: 0024_raw_inputs_tsv_sender
Create Date: 2026-07-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0025_chat_conversations"
down_revision = "0024_raw_inputs_tsv_sender"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("messages", JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_chat_conversations_updated_at",
        "chat_conversations",
        [sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_conversations_updated_at", table_name="chat_conversations")
    op.drop_table("chat_conversations")
