"""notes: generated tsvector column for hybrid (vector + keyword) search

The notes lookup goes hybrid — pgvector similarity fused with Postgres FTS via
RRF, the same shape as the calendar lookup. FTS needs a materialized `tsv`
(notes can carry long content), so add it as a generated column with its own
GIN index. The trigram index on `content` (0019) stays for prefix/typo matching
elsewhere.

Revision ID: 0021_notes_tsv
Revises: 0020_points_amount_integer
Create Date: 2026-07-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0021_notes_tsv"
down_revision = "0020_points_amount_integer"
branch_labels = None
depends_on = None

_TSV_EXPR = "to_tsvector('english', coalesce(content, ''))"


def upgrade() -> None:
    op.add_column(
        "notes",
        sa.Column("tsv", TSVECTOR, sa.Computed(_TSV_EXPR, persisted=True), nullable=True),
    )
    op.create_index("ix_notes_tsv", "notes", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_notes_tsv", table_name="notes")
    op.drop_column("notes", "tsv")
