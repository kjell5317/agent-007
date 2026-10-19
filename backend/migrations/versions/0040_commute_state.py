"""Where the bike and the user are — folded from finished commute legs, so
the legs themselves can be deleted once they end."""

import sqlalchemy as sa
from alembic import op

revision = "0040_commute_state"
down_revision = "0039_commute_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commute_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bike_location", sa.Text(), nullable=True),
        sa.Column("bike_anchor", sa.String(256), nullable=False, server_default="home"),
        sa.Column("bike_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_location", sa.Text(), nullable=True),
        sa.Column("user_anchor", sa.String(256), nullable=False, server_default="home"),
        sa.Column("user_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("commute_state")
