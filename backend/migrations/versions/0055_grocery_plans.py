"""Grocery settings and weekly recipes; storage remembers the amount it
started with, so a card can show how many portions are left of it."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055_grocery_plans"
down_revision = "0054_groceries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("storage_items", sa.Column("start_quantity", sa.Float(), nullable=True))
    op.execute("UPDATE storage_items SET start_quantity = quantity")
    op.alter_column("storage_items", "start_quantity", nullable=False)

    op.create_table(
        "grocery_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("diet", sa.String(16), nullable=False, server_default="omnivore"),
        sa.Column("recipes_per_week", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("servings", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("store_location", sa.String(512), nullable=True),
    )
    op.execute("INSERT INTO grocery_settings (id) VALUES (1)")

    op.create_table(
        "grocery_recipes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("servings", sa.Integer(), nullable=False),
        sa.Column("ingredients", sa.JSON(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_grocery_recipes_week_start", "grocery_recipes", ["week_start"])


def downgrade() -> None:
    op.drop_table("grocery_recipes")
    op.drop_table("grocery_settings")
    op.drop_column("storage_items", "start_quantity")
