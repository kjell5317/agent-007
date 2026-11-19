"""Shopping list → storage → used up, removed or expired. A starred product
goes straight back on the list once its last portion leaves storage, and the
shopping task is due the next day. Using or removing something by hand starts
the quick re-check (see `recheck.py`); crossing off the last list entry
closes the shopping task."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.clients import grocery as grocery_store
from app.db.models.grocery import GroceryItem, GroceryProduct, StorageItem
from app.db.schemas.grocery import (
    GroceryItemRead,
    GroceryItemUpdate,
    GroceryState,
    StorageItemRead,
    StorageItemUpdate,
)
from app.services.grocery.normalize import (
    after_portion,
    entry_quantity,
    expiry_date,
    parse_entry,
    product_key,
    shelf_life_from,
)
from app.services.grocery import recheck, shopping
from app.services.grocery.products import create_named, resolve
from app.timezones import to_user_tz


async def state(session: Session) -> GroceryState:
    await purge_expired(session)
    return GroceryState(
        items=[GroceryItemRead.model_validate(row) for row in grocery_store.open_items(session)],
        storage=[
            StorageItemRead.model_validate(row) for row in grocery_store.active_storage(session)
        ],
    )


async def add_entry(session: Session, text: str) -> None:
    entry = parse_entry(text)
    product = await resolve(session, entry.name)
    quantity = entry_quantity(entry, unit=product.unit, pack_quantity=product.pack_quantity)
    listed = grocery_store.open_item_for(session, product.key)
    if listed is None:
        grocery_store.add_item(session, product=product, quantity=quantity, origin="manual")
    else:
        # Typed again, it's the user's own entry now; only an explicit
        # amount adds to what's listed.
        listed.origin = "manual"
        if entry.amount is not None:
            listed.quantity += quantity
    session.commit()


async def update_item(session: Session, item_id: uuid.UUID, edit: GroceryItemUpdate) -> None:
    item = _open_item(session, item_id)
    await _edit(
        session, item, name=edit.name, quantity=edit.quantity, unit=edit.unit, portion=edit.portion
    )
    if edit.category is not None:
        item.category = edit.category
        item.product.category = edit.category
    session.commit()


def delete_item(session: Session, item_id: uuid.UUID) -> None:
    grocery_store.delete(session, _open_item(session, item_id))
    session.commit()


async def cross_off(session: Session, item_id: uuid.UUID) -> None:
    item = _open_item(session, item_id)
    now = datetime.now(timezone.utc)
    item.bought_at = now
    grocery_store.add_storage(
        session,
        item=item,
        expires_on=expiry_date(_today(now), item.product.shelf_life_days),
    )
    session.commit()
    await shopping.close_if_done(session)


def uncross(session: Session, item_id: uuid.UUID) -> None:
    item = grocery_store.get_item(session, item_id)
    if item is None or item.bought_at is None:
        raise LookupError("Item not found")
    stored = grocery_store.storage_for_item(session, item.id)
    if stored is not None:
        grocery_store.delete(session, stored)
    item.bought_at = None
    session.commit()


async def use_portion(session: Session, storage_id: uuid.UUID) -> None:
    stored = _active_storage(session, storage_id)
    now = datetime.now(timezone.utc)
    left = after_portion(stored.quantity, stored.portion)
    restocked = False
    if left is None:
        restocked = _take_out(session, stored, "used_up", now)
        change = f"Used the last of {stored.name}"
    else:
        stored.quantity = left
        change = (
            f"Used {_amount(stored.portion, stored.unit)} of {stored.name}, "
            f"{_amount(left, stored.unit)} left"
        )
    session.commit()
    recheck.note(change)
    if restocked:
        await _shop_tomorrow(session, now)


async def remove(session: Session, storage_id: uuid.UUID) -> None:
    stored = _active_storage(session, storage_id)
    now = datetime.now(timezone.utc)
    restocked = _take_out(session, stored, "removed", now)
    session.commit()
    recheck.note(
        f"Took {stored.name} out of storage with {_amount(stored.quantity, stored.unit)} left"
    )
    if restocked:
        await _shop_tomorrow(session, now)


async def update_storage(
    session: Session, storage_id: uuid.UUID, edit: StorageItemUpdate
) -> None:
    stored = _active_storage(session, storage_id)
    unit = stored.unit
    await _edit(
        session,
        stored,
        name=edit.name,
        quantity=edit.quantity,
        unit=edit.unit,
        portion=edit.portion,
    )
    # A new amount left is the bar's new 100%.
    if edit.quantity is not None or stored.unit != unit:
        stored.start_quantity = stored.quantity
    if edit.expires_on is not None:
        stored.expires_on = edit.expires_on
        # The user's date is the better estimate for the next purchase too.
        stored.product.shelf_life_days = shelf_life_from(
            _today(stored.added_at), edit.expires_on
        )
    session.commit()


def set_staple(session: Session, product_key: str, staple: bool) -> None:
    product = grocery_store.get_product(session, product_key)
    if product is None:
        raise LookupError("Product not found")
    product.staple = staple
    session.commit()


async def purge_expired(session: Session) -> int:
    now = datetime.now(timezone.utc)
    expired = grocery_store.expired_storage(session, _today(now))
    restocked = False
    for stored in expired:
        restocked = _take_out(session, stored, "expired", now) or restocked
    if expired:
        session.commit()
    if restocked:
        await _shop_tomorrow(session, now)
    return len(expired)


def _take_out(session: Session, stored: StorageItem, reason: str, now: datetime) -> bool:
    """True when a starred product went back on the list."""
    stored.removed_at = now
    stored.removed_reason = reason
    session.flush()
    return _restock(session, stored.product)


def _restock(session: Session, product: GroceryProduct) -> bool:
    if not product.staple:
        return False
    if grocery_store.in_storage(session, product.key):
        return False
    if grocery_store.open_item_for(session, product.key) is not None:
        return False
    grocery_store.add_item(
        session, product=product, quantity=product.pack_quantity, origin="staple"
    )
    return True


async def _shop_tomorrow(session: Session, now: datetime) -> None:
    # A starred product ran out: it's needed, but not this very hour.
    await shopping.ensure_task(session, _today(now) + timedelta(days=1))


def _amount(quantity: float, unit: str) -> str:
    return f"{round(quantity, 2):g} {unit}"


async def _edit(
    session: Session,
    row: GroceryItem | StorageItem,
    *,
    name: str | None,
    quantity: float | None,
    unit: str | None,
    portion: float | None,
) -> None:
    """Rename first — it can move the row to another product — then apply the
    amounts on top. Unit and portion are the product's from now on."""
    if name is not None and name.strip() != row.name:
        await _rename(session, row, name.strip())
    product = row.product
    if unit is not None and unit != row.unit:
        if portion is None:
            raise ValueError("Set the portion in the new unit too")
        row.unit = product.unit = unit
        # The usual amount was counted in the old unit.
        product.pack_quantity = quantity or row.quantity
    if quantity is not None:
        row.quantity = quantity
    if portion is not None:
        row.portion = product.portion = portion


async def _rename(session: Session, row: GroceryItem | StorageItem, name: str) -> None:
    key = product_key(name)
    if not key:
        raise ValueError("Enter a product name")
    current = row.product
    if key == current.key:
        current.name = name
    else:
        # The word was resolved to the wrong product ("Eis" → Eggs): it stops
        # resolving there, and gets a product of its own.
        grocery_store.remove_alias(session, current, key)
        target = grocery_store.find_product(session, key) or await create_named(session, name)
        _move(row, target)
    row.name = name


def _move(row: GroceryItem | StorageItem, product: GroceryProduct) -> None:
    if row.unit != product.unit:
        row.quantity = product.pack_quantity
    row.product = product
    row.product_key = product.key
    row.unit = product.unit
    row.portion = product.portion
    row.category = product.category


def _open_item(session: Session, item_id: uuid.UUID) -> GroceryItem:
    item = grocery_store.get_item(session, item_id)
    if item is None or item.bought_at is not None:
        raise LookupError("Item not found")
    return item


def _active_storage(session: Session, storage_id: uuid.UUID) -> StorageItem:
    stored = grocery_store.get_storage(session, storage_id)
    if stored is None or stored.removed_at is not None:
        raise LookupError("Storage item not found")
    return stored


def _today(moment: datetime) -> date:
    return to_user_tz(moment).date()
