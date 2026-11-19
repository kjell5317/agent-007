from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.grocery import GroceryItem, GroceryProduct, StorageItem


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
    session: Session, *, product: GroceryProduct, quantity: float, origin: str
) -> GroceryItem:
    row = GroceryItem(
        product_key=product.key,
        name=product.name,
        quantity=quantity,
        unit=product.unit,
        portion=product.portion,
        category=product.category,
        origin=origin,
        product=product,
    )
    session.add(row)
    session.flush()
    return row


def delete(session: Session, row: GroceryItem | StorageItem) -> None:
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
