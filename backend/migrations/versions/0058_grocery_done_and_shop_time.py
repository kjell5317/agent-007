"""Recipes can be marked cooked (they move to the archive), and the store
gets the time of day the shopping task is due by."""

import sqlalchemy as sa
from alembic import op

revision = "0058_grocery_done_and_shop_time"
down_revision = "0057_grocery_shopping_task"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "grocery_settings",
        sa.Column("shop_by_time", sa.Time(), nullable=False, server_default="18:00"),
    )
    op.add_column(
        "grocery_recipes", sa.Column("done_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("grocery_recipes", "done_at")
    op.drop_column("grocery_settings", "shop_by_time")
