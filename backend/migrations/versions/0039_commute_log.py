"""Commute mode flips — the training signal for commute preferences and
the pins the planner honours."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039_commute_log"
down_revision = "0038_scheduling_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commute_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("origin_anchor", sa.String(256), nullable=False),
        sa.Column("dest_anchor", sa.String(256), nullable=False),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("destination", sa.Text(), nullable=True),
        sa.Column("depart", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrive", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_mode", sa.String(16), nullable=False),
        sa.Column("flipped_mode", sa.String(16), nullable=False),
        sa.Column("past", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("features", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_commute_log_depart", "commute_log", ["depart"])
    op.create_index("ix_commute_log_leg", "commute_log", ["origin_anchor", "dest_anchor"])


def downgrade() -> None:
    op.drop_index("ix_commute_log_leg", table_name="commute_log")
    op.drop_index("ix_commute_log_depart", table_name="commute_log")
    op.drop_table("commute_log")
