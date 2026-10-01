"""Save search-result open counts and the latest result details."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0033_search_result_clicks"
down_revision = "0032_parent_subtask_deadlines"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_search_result_clicks_popular", table_name="search_result_clicks")
    op.drop_table("search_result_clicks")
