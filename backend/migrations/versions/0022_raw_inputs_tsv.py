"""raw_inputs: generated tsvector column for hybrid precedent search

Precedent lookup (`raw_inputs.search_similar`) goes hybrid — pgvector similarity
fused with Postgres FTS via RRF. The keyword side runs over subject + content,
so add a materialized `tsv` generated column + GIN index. (This feeds precedent
*retrieval* for the agent; the auto-decide gate still compares raw cosine.)

Revision ID: 0022_raw_inputs_tsv
Revises: 0021_notes_tsv
Create Date: 2026-07-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0022_raw_inputs_tsv"
down_revision = "0021_notes_tsv"
branch_labels = None
depends_on = None

# Subject (from the source metadata) + body. `->>` and the 2-arg to_tsvector are
# immutable, so this is valid as a STORED generated column.
_TSV_EXPR = (
    "to_tsvector('english', "
    "coalesce(source_metadata->>'subject', '') || ' ' || coalesce(content, ''))"
)


def upgrade() -> None:
    op.add_column(
        "raw_inputs",
        sa.Column("tsv", TSVECTOR, sa.Computed(_TSV_EXPR, persisted=True), nullable=True),
    )
    op.create_index("ix_raw_inputs_tsv", "raw_inputs", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_raw_inputs_tsv", table_name="raw_inputs")
    op.drop_column("raw_inputs", "tsv")
