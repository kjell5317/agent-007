"""The recipes a planned shopping-list entry is for, so the list can mark it."""

import sqlalchemy as sa
from alembic import op

revision = "0056_grocery_item_recipes"
down_revision = "0055_grocery_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "grocery_items",
        sa.Column("recipe_titles", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("grocery_items", "recipe_titles")
