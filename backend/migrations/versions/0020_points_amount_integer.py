"""points_entries: amount is a whole number

Points are always integers now. Round any existing fractional amounts (kotx
awards used a 0.1 factor, so odd 5-minute estimates produced x.5 values) and
change the column type to Integer so the invariant is enforced by the schema.
`factor`/`quantity` stay Float — they legitimately hold 0.1, minutes, etc.

Revision ID: 0020_points_amount_integer
Revises: 0019_documents_search
Create Date: 2026-07-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_points_amount_integer"
down_revision = "0019_documents_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # round(amount::numeric) rounds halves away from zero — matching the
    # runtime `_whole` helper — before the cast to integer.
    op.alter_column(
        "points_entries",
        "amount",
        existing_type=sa.Float(),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="round(amount::numeric)::integer",
    )


def downgrade() -> None:
    op.alter_column(
        "points_entries",
        "amount",
        existing_type=sa.Integer(),
        type_=sa.Float(),
        existing_nullable=False,
        postgresql_using="amount::double precision",
    )
