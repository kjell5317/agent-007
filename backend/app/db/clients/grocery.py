from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.grocery import (
    GroceryItem,
    GroceryProduct,
    GroceryRecipe,
    GrocerySettings,
    StorageItem,
)

_SETTINGS_ID = 1


def find_product(session: Session, key: str) -> GroceryProduct | None:
    stmt = select(GroceryProduct).where(
        or_(GroceryProduct.key == key, GroceryProduct.aliases.any(key))
    )
    return session.execute(stmt.limit(1)).scalars().first()


def get_product(session: Session, key: str) -> GroceryProduct | None:
    return session.get(GroceryProduct, key)


def add_product(
    session: Session,
    *,
    key: str,
    alias: str | None,
    name: str,
    category: str,
    unit: str,
    pack_quantity: float,
    portion: float,
    shelf_life_days: int,
) -> GroceryProduct:
    row = GroceryProduct(
        key=key,
        aliases=[alias] if alias and alias != key else [],
        name=name,
        category=category,
        unit=unit,
        pack_quantity=pack_quantity,
        portion=portion,
        shelf_life_days=shelf_life_days,
        staple=False,
    )
    session.add(row)
    session.flush()
    return row


def add_alias(product: GroceryProduct, alias: str) -> None:
    if alias != product.key and alias not in product.aliases:
        product.aliases = [*product.aliases, alias]


def remove_alias(session: Session, product: GroceryProduct, alias: str) -> None:
    if alias in product.aliases:
        product.aliases = [a for a in product.aliases if a != alias]
        # The session doesn't autoflush; the next lookup must not find it.
        session.flush()


def open_items(session: Session) -> list[GroceryItem]:
    stmt = (
        select(GroceryItem)
        .where(GroceryItem.bought_at.is_(None))
        .order_by(GroceryItem.created_at)
    )
    return list(session.execute(stmt).scalars())


def get_item(session: Session, item_id: uuid.UUID) -> GroceryItem | None:
    return session.get(GroceryItem, item_id)


def open_item_for(session: Session, product_key: str) -> GroceryItem | None:
    stmt = select(GroceryItem).where(
        GroceryItem.product_key == product_key, GroceryItem.bought_at.is_(None)
    )
    return session.execute(stmt.limit(1)).scalars().first()


def add_item(
    session: Session,
    *,
    product: GroceryProduct,
    quantity: float,
    origin: str,
    recipe_titles: list[str] | None = None,
) -> GroceryItem:
    row = GroceryItem(
        product_key=product.key,
        name=product.name,
        quantity=quantity,
        unit=product.unit,
        portion=product.portion,
        category=product.category,
        origin=origin,
        recipe_titles=recipe_titles or [],
        product=product,
    )
    session.add(row)
    session.flush()
    return row


def delete(session: Session, row: GroceryItem | StorageItem | GroceryRecipe) -> None:
    session.delete(row)
    session.flush()


def active_storage(session: Session) -> list[StorageItem]:
    stmt = (
        select(StorageItem)
        .where(StorageItem.removed_at.is_(None))
        .order_by(StorageItem.expires_on, StorageItem.name)
    )
    return list(session.execute(stmt).scalars())


def get_storage(session: Session, storage_id: uuid.UUID) -> StorageItem | None:
    return session.get(StorageItem, storage_id)


def storage_for_item(session: Session, item_id: uuid.UUID) -> StorageItem | None:
    stmt = select(StorageItem).where(
        StorageItem.grocery_item_id == item_id, StorageItem.removed_at.is_(None)
    )
    return session.execute(stmt.limit(1)).scalars().first()


def stock_of(session: Session, product_key: str) -> list[StorageItem]:
    stmt = select(StorageItem).where(
        StorageItem.product_key == product_key, StorageItem.removed_at.is_(None)
    )
    return list(session.execute(stmt).scalars())


def in_storage(session: Session, product_key: str) -> bool:
    stmt = select(StorageItem.id).where(
        StorageItem.product_key == product_key, StorageItem.removed_at.is_(None)
    )
    return session.execute(stmt.limit(1)).first() is not None


def add_storage(session: Session, *, item: GroceryItem, expires_on: date) -> StorageItem:
    row = StorageItem(
        product_key=item.product_key,
        grocery_item_id=item.id,
        name=item.name,
        quantity=item.quantity,
        start_quantity=item.quantity,
        unit=item.unit,
        portion=item.portion,
        category=item.category,
        expires_on=expires_on,
        product=item.product,
    )
    session.add(row)
    session.flush()
    return row


def expired_storage(session: Session, today: date) -> list[StorageItem]:
    stmt = select(StorageItem).where(
        StorageItem.removed_at.is_(None), StorageItem.expires_on < today
    )
    return list(session.execute(stmt).scalars())


def bought_since(session: Session, since: datetime) -> list[GroceryItem]:
    stmt = select(GroceryItem).where(GroceryItem.bought_at >= since)
    return list(session.execute(stmt).scalars())


def expired_since(session: Session, since: datetime) -> list[StorageItem]:
    stmt = select(StorageItem).where(
        StorageItem.removed_reason == "expired", StorageItem.removed_at >= since
    )
    return list(session.execute(stmt).scalars())


def settings(session: Session) -> GrocerySettings:
    row = session.get(GrocerySettings, _SETTINGS_ID)
    if row is None:
        row = GrocerySettings(id=_SETTINGS_ID, diet="omnivore", recipes_per_week=0, servings=1)
        session.add(row)
        session.flush()
    return row


def recipes_for_week(session: Session, week_start: date) -> list[GroceryRecipe]:
    stmt = (
        select(GroceryRecipe)
        .where(GroceryRecipe.week_start == week_start)
        .order_by(GroceryRecipe.created_at, GroceryRecipe.title)
    )
    return list(session.execute(stmt).scalars())


def archived_recipes(session: Session, week_start: date, *, limit: int) -> list[GroceryRecipe]:
    """Past weeks, and this week's recipes already cooked; newest week first."""
    stmt = (
        select(GroceryRecipe)
        .where(or_(GroceryRecipe.week_start < week_start, GroceryRecipe.done_at.is_not(None)))
        .order_by(
            GroceryRecipe.week_start.desc(),
            GroceryRecipe.done_at.desc().nulls_last(),
            GroceryRecipe.created_at,
        )
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def rated_recipes(session: Session, rating: int, *, limit: int) -> list[GroceryRecipe]:
    stmt = (
        select(GroceryRecipe)
        .where(GroceryRecipe.rating == rating)
        .order_by(GroceryRecipe.week_start.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def get_recipe(session: Session, recipe_id: uuid.UUID) -> GroceryRecipe | None:
    return session.get(GroceryRecipe, recipe_id)


def add_recipe(
    session: Session,
    *,
    week_start: date,
    title: str,
    servings: int,
    ingredients: list[dict[str, Any]],
    steps: list[str],
) -> GroceryRecipe:
    row = GroceryRecipe(
        week_start=week_start,
        title=title,
        servings=servings,
        ingredients=ingredients,
        steps=steps,
    )
    session.add(row)
    session.flush()
    return row
