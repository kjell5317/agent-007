"""Drop search-result open counts: the popular-results view they fed is gone.
The downgrade restores the empty table, not the counts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0053_drop_search_result_clicks"
down_revision = "0052_work_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_search_result_clicks_popular", table_name="search_result_clicks")
    op.drop_table("search_result_clicks")


def downgrade() -> None:
    op.create_table(
        "search_result_clicks",
        sa.Column("hit_type", sa.Text, primary_key=True),
        sa.Column("hit_id", sa.Text, primary_key=True),
        sa.Column("hit", JSONB, nullable=False),
        sa.Column("click_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_clicked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_search_result_clicks_popular",
        "search_result_clicks",
        [sa.text("click_count DESC"), sa.text("last_clicked_at DESC")],
    )
