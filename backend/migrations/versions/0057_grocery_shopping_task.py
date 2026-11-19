"""The open shopping task the grocery agent created, so it is brought forward
or closed instead of duplicated."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057_grocery_shopping_task"
down_revision = "0056_grocery_item_recipes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "grocery_settings",
        sa.Column(
            "shopping_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("grocery_settings", "shopping_task_id")
