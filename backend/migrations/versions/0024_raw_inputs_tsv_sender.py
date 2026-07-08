"""raw_inputs: fold sender + channel into the tsvector

Suggest now searches sender (`from`) and Slack `channel_name`, and matches
inputs against the stored `tsv` for indexed FTS. Rebuild the generated column to
include those fields (a STORED generated column's expression can't be altered in
place, so drop + re-add). Also widens the precedent hybrid's keyword side, which
shares this column.

Revision ID: 0024_raw_inputs_tsv_sender
Revises: 0023_tasks_tsv
Create Date: 2026-07-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0024_raw_inputs_tsv_sender"
down_revision = "0023_tasks_tsv"
branch_labels = None
depends_on = None

_WITH_SENDER = (
    "to_tsvector('english', "
    "coalesce(source_metadata->>'subject', '') || ' ' || "
    "coalesce(source_metadata->>'from', '') || ' ' || "
    "coalesce(source_metadata->>'channel_name', '') || ' ' || "
    "coalesce(content, ''))"
)
_SUBJECT_CONTENT_ONLY = (
    "to_tsvector('english', "
    "coalesce(source_metadata->>'subject', '') || ' ' || coalesce(content, ''))"
)


def _rebuild(expr: str) -> None:
    op.drop_index("ix_raw_inputs_tsv", table_name="raw_inputs")
    op.drop_column("raw_inputs", "tsv")
    op.add_column(
        "raw_inputs",
        sa.Column("tsv", TSVECTOR, sa.Computed(expr, persisted=True), nullable=True),
    )
    op.create_index("ix_raw_inputs_tsv", "raw_inputs", ["tsv"], postgresql_using="gin")


def upgrade() -> None:
    _rebuild(_WITH_SENDER)


def downgrade() -> None:
    _rebuild(_SUBJECT_CONTENT_ONLY)
