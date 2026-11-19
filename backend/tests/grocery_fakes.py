"""An in-memory stand-in for `app.db.clients.grocery`, for the grocery
service tests."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.db.clients import grocery as grocery_store


class FakeSession:
    def commit(self):
        pass

    def flush(self):
        pass


class FakeStore:
    def __init__(self):
        self.products: dict[str, SimpleNamespace] = {}
        self.items: list[SimpleNamespace] = []
        self.storage: list[SimpleNamespace] = []
        self.recipes: list[SimpleNamespace] = []

    def product(self, key, **fields):
        row = SimpleNamespace(
            key=key, name=key.title(), category="other", unit="piece",
            pack_quantity=1, portion=1, shelf_life_days=7, staple=False, aliases=[],
        )
        row.__dict__.update(fields)
        self.products[key] = row
        return row

    def stored(self, product, *, quantity=None, expires_on=None, added_at=None):
        amount = quantity if quantity is not None else product.pack_quantity
        row = SimpleNamespace(
            id=uuid.uuid4(), product_key=product.key, product=product, grocery_item_id=None,
            name=product.name, quantity=amount, start_quantity=amount, unit=product.unit,
            portion=product.portion, category=product.category,
            expires_on=expires_on or date(2099, 1, 1),
            added_at=added_at or datetime.now(timezone.utc),
            removed_at=None, removed_reason=None,
        )
        self.storage.append(row)
        return row

    def get_product(self, session, key):
        return self.products.get(key)

    def find_product(self, session, key):
        return next(
            (p for p in self.products.values() if key == p.key or key in p.aliases), None
        )

    def add_product(self, session, *, key, alias, **facts):
        return self.product(key, aliases=[alias] if alias and alias != key else [], **facts)

    def open_items(self, session):
        return [i for i in self.items if i.bought_at is None]

    def get_item(self, session, item_id):
        return next((i for i in self.items if i.id == item_id), None)

    def open_item_for(self, session, product_key):
        return next(
            (i for i in self.open_items(session) if i.product_key == product_key), None
        )

    def add_item(self, session, *, product, quantity, origin, recipe_titles=None):
        row = SimpleNamespace(
            id=uuid.uuid4(), product_key=product.key, product=product, name=product.name,
            quantity=quantity, unit=product.unit, portion=product.portion,
            category=product.category, origin=origin, bought_at=None,
            recipe_titles=recipe_titles or [],
        )
        self.items.append(row)
        return row

    def delete(self, session, row):
        for rows in (self.items, self.storage, self.recipes):
            if row in rows:
                rows.remove(row)

    def get_storage(self, session, storage_id):
        return next((s for s in self.storage if s.id == storage_id), None)

    def storage_for_item(self, session, item_id):
        return next(
            (s for s in self.storage if s.grocery_item_id == item_id and s.removed_at is None),
            None,
        )

    def stock_of(self, session, product_key):
        return [s for s in self.storage if s.product_key == product_key and s.removed_at is None]

    def in_storage(self, session, product_key):
        return any(s.product_key == product_key and s.removed_at is None for s in self.storage)

    def add_storage(self, session, *, item, expires_on):
        row = self.stored(item.product, quantity=item.quantity, expires_on=expires_on)
        row.grocery_item_id = item.id
        return row

    def expired_storage(self, session, today):
        return [s for s in self.storage if s.removed_at is None and s.expires_on < today]

    def recipes_for_week(self, session, week_start):
        return [r for r in self.recipes if r.week_start == week_start]

    def add_recipe(self, session, **fields):
        row = SimpleNamespace(id=uuid.uuid4(), rating=None, done_at=None, **fields)
        self.recipes.append(row)
        return row


def patch_store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    for name in (
        "get_product", "find_product", "add_product", "open_items", "get_item",
        "open_item_for", "add_item", "delete", "get_storage", "storage_for_item",
        "in_storage", "stock_of", "add_storage", "expired_storage", "recipes_for_week",
        "add_recipe",
    ):
        monkeypatch.setattr(grocery_store, name, getattr(fake, name))
    return fake
