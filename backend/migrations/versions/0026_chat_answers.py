"""chat_answers: semantic cache of completed read-only chat Q→A pairs

Every chat turn already embeds its question. Persist the clean, read-only ones
(question, question-embedding, answer) so a later similarly-phrased question can
pull the prior answer into context as a hint. Matched by cosine only; bounded by
age at query time, so no keyword/tsv column and no vector index (the table stays
small — a personal cache — so a sequential cosine scan is fine, matching notes).

Revision ID: 0026_chat_answers
Revises: 0025_chat_conversations
Create Date: 2026-07-18
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID

revision = "0026_chat_answers"
down_revision = "0025_chat_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_answers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_chat_answers_created_at", "chat_answers", [sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_chat_answers_created_at", table_name="chat_answers")
    op.drop_table("chat_answers")
