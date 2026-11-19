import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class GroceryProduct(Base):
    """The product cache: how a product is usually bought, how much of it is
    used at once and how long it keeps, so a repeat entry needs no LLM call.
    `key` is the normalized canonical name; `aliases` holds the other
    spellings typed for it ("tomato", "tomaten"), so they share one row and
    one star. The user's corrections (category, expiry) overwrite it."""

    __tablename__ = "grocery_products"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), default=list, server_default="{}"
    )
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(16))
    unit: Mapped[str] = mapped_column(String(8))
    pack_quantity: Mapped[float] = mapped_column(Float)
    portion: Mapped[float] = mapped_column(Float)
    shelf_life_days: Mapped[int] = mapped_column(Integer)
    # Put back on the list as soon as it runs out.
    staple: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroceryItem(Base):
    """An entry on the shopping list. Crossing it off keeps the row with
    `bought_at` set — the purchase history the planner reads."""

    __tablename__ = "grocery_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_key: Mapped[str] = mapped_column(
        String(128), ForeignKey("grocery_products.key"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8))
    portion: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(16))
    # manual | agent | staple
    origin: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    bought_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    product: Mapped[GroceryProduct] = relationship(lazy="joined")

    @property
    def staple(self) -> bool:
        return self.product.staple


class StorageItem(Base):
    """Something at home. `quantity` counts down one `portion` at a time; a
    row leaves storage by setting `removed_at`, so what was used up and what
    expired stays countable."""

    __tablename__ = "storage_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_key: Mapped[str] = mapped_column(
        String(128), ForeignKey("grocery_products.key"), index=True
    )
    grocery_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("grocery_items.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[float] = mapped_column(Float)
    # What the bar of portions counts from: the amount bought, or the amount
    # set by hand when it was raised or the unit changed.
    start_quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8))
    portion: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(16))
    expires_on: Mapped[date] = mapped_column(Date)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # used_up | removed | expired
    removed_reason: Mapped[str | None] = mapped_column(String(8), nullable=True)

    product: Mapped[GroceryProduct] = relationship(lazy="joined")

    @property
    def staple(self) -> bool:
        return self.product.staple


class GrocerySettings(Base):
    """One row (id 1): what the weekly planner cooks for."""

    __tablename__ = "grocery_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # vegan | vegetarian | omnivore
    diet: Mapped[str] = mapped_column(String(16), default="omnivore", server_default="omnivore")
    recipes_per_week: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    servings: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    store_location: Mapped[str | None] = mapped_column(String(512), nullable=True)


class GroceryRecipe(Base):
    """A recipe planned for the week starting on `week_start` (a Monday).
    Past weeks are the archive; their `rating` (1 liked, -1 disliked) steers
    the next plans."""

    __tablename__ = "grocery_recipes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    week_start: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(200))
    servings: Mapped[int] = mapped_column(Integer)
    # [{"name": str, "amount": str}]
    ingredients: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    steps: Mapped[list[str]] = mapped_column(JSON)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
