"""Groceries: the product cache, the shopping list and what's in storage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054_groceries"
down_revision = "0053_drop_search_result_clicks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grocery_products",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column(
            "aliases", postgresql.ARRAY(sa.String(128)), nullable=False, server_default="{}"
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("pack_quantity", sa.Float(), nullable=False),
        sa.Column("portion", sa.Float(), nullable=False),
        sa.Column("shelf_life_days", sa.Integer(), nullable=False),
        sa.Column("staple", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "grocery_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "product_key",
            sa.String(128),
            sa.ForeignKey("grocery_products.key"),
            nullable=False,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("portion", sa.Float(), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("bought_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_grocery_items_product_key", "grocery_items", ["product_key"])
    op.create_index("ix_grocery_items_bought_at", "grocery_items", ["bought_at"])
    op.create_table(
        "storage_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "product_key",
            sa.String(128),
            sa.ForeignKey("grocery_products.key"),
            nullable=False,
        ),
        sa.Column(
            "grocery_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("grocery_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(8), nullable=False),
        sa.Column("portion", sa.Float(), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_reason", sa.String(8), nullable=True),
    )
    op.create_index("ix_storage_items_product_key", "storage_items", ["product_key"])
    op.create_index("ix_storage_items_removed_at", "storage_items", ["removed_at"])


def downgrade() -> None:
    op.drop_table("storage_items")
    op.drop_table("grocery_items")
    op.drop_table("grocery_products")
