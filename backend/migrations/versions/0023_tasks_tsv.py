"""tasks: generated tsvector column for indexed suggest search

Stage-1 suggest now matches every corpus against a stored, GIN-indexed `tsv`
(index-backed FTS instead of a per-row `to_tsvector` scan). tasks had none, so
add one over title + description + label.

Revision ID: 0023_tasks_tsv
Revises: 0022_raw_inputs_tsv
Create Date: 2026-07-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0023_tasks_tsv"
down_revision = "0022_raw_inputs_tsv"
branch_labels = None
depends_on = None

_TSV_EXPR = (
    "to_tsvector('english', coalesce(title,'') || ' ' || "
    "coalesce(description,'') || ' ' || coalesce(label,''))"
)


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("tsv", TSVECTOR, sa.Computed(_TSV_EXPR, persisted=True), nullable=True),
    )
    op.create_index("ix_tasks_tsv", "tasks", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_tasks_tsv", table_name="tasks")
    op.drop_column("tasks", "tsv")
