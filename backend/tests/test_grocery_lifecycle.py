"""Shopping list → storage → used up or expired, against an in-memory store."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.clients import grocery as grocery_store
from app.services.grocery import lifecycle, products
from app.timezones import to_user_tz


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

    def product(self, key, **fields):
        row = SimpleNamespace(
            key=key, name=key.title(), category="other", unit="piece",
            pack_quantity=1, portion=1, shelf_life_days=7, staple=False, aliases=[],
        )
        row.__dict__.update(fields)
        self.products[key] = row
        return row

    def stored(self, product, *, quantity=None, expires_on=None, added_at=None):
        row = SimpleNamespace(
            id=uuid.uuid4(), product_key=product.key, product=product, grocery_item_id=None,
            quantity=quantity if quantity is not None else product.pack_quantity,
            portion=product.portion, expires_on=expires_on or date(2099, 1, 1),
            added_at=added_at or datetime.now(timezone.utc),
            removed_at=None, removed_reason=None,
        )
        self.storage.append(row)
        return row

    def get_product(self, session, key):
        return self.products.get(key)

    def open_items(self, session):
        return [i for i in self.items if i.bought_at is None]

    def get_item(self, session, item_id):
        return next((i for i in self.items if i.id == item_id), None)

    def open_item_for(self, session, product_key):
        return next(
            (i for i in self.open_items(session) if i.product_key == product_key), None
        )

    def add_item(self, session, *, product, quantity, origin):
        row = SimpleNamespace(
            id=uuid.uuid4(), product_key=product.key, product=product, name=product.name,
            quantity=quantity, unit=product.unit, portion=product.portion,
            category=product.category, origin=origin, bought_at=None,
        )
        self.items.append(row)
        return row

    def delete(self, session, row):
        if row in self.items:
            self.items.remove(row)
        else:
            self.storage.remove(row)

    def get_storage(self, session, storage_id):
        return next((s for s in self.storage if s.id == storage_id), None)

    def storage_for_item(self, session, item_id):
        return next(
            (s for s in self.storage if s.grocery_item_id == item_id and s.removed_at is None),
            None,
        )

    def in_storage(self, session, product_key):
        return any(s.product_key == product_key and s.removed_at is None for s in self.storage)

    def add_storage(self, session, *, item, expires_on):
        row = self.stored(item.product, quantity=item.quantity, expires_on=expires_on)
        row.grocery_item_id = item.id
        return row

    def expired_storage(self, session, today):
        return [s for s in self.storage if s.removed_at is None and s.expires_on < today]


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    for name in (
        "get_product", "open_items", "get_item", "open_item_for", "add_item", "delete",
        "get_storage", "storage_for_item", "in_storage", "add_storage", "expired_storage",
    ):
        monkeypatch.setattr(grocery_store, name, getattr(fake, name))
    return fake


def _today():
    return to_user_tz(datetime.now(timezone.utc)).date()


def test_cross_off_moves_the_item_into_storage(store):
    rice = store.product("rice", unit="g", pack_quantity=1000, portion=150, shelf_life_days=365)
    item = store.add_item(None, product=rice, quantity=500, origin="manual")

    lifecycle.cross_off(FakeSession(), item.id)

    assert item.bought_at is not None
    [stored] = store.storage
    assert stored.grocery_item_id == item.id
    assert stored.quantity == 500
    assert stored.expires_on == _today() + timedelta(days=365)


def test_uncross_takes_it_back_out_of_storage(store):
    rice = store.product("rice")
    item = store.add_item(None, product=rice, quantity=1, origin="manual")
    lifecycle.cross_off(FakeSession(), item.id)

    lifecycle.uncross(FakeSession(), item.id)

    assert item.bought_at is None
    assert store.storage == []


def test_portions_count_down_until_used_up(store):
    rice = store.product("rice", unit="g", pack_quantity=1000, portion=400)
    stored = store.stored(rice)

    lifecycle.use_portion(FakeSession(), stored.id)
    lifecycle.use_portion(FakeSession(), stored.id)
    assert stored.quantity == pytest.approx(200)
    assert stored.removed_at is None

    lifecycle.use_portion(FakeSession(), stored.id)
    assert stored.removed_reason == "used_up"
    assert store.items == []  # not a staple


def test_a_staple_goes_back_on_the_list_once_the_last_one_is_used(store):
    milk = store.product("milk", unit="ml", pack_quantity=1000, portion=1000, staple=True)
    first, second = store.stored(milk), store.stored(milk)

    lifecycle.use_portion(FakeSession(), first.id)
    assert store.items == []  # another carton is still at home

    lifecycle.remove(FakeSession(), second.id)
    [item] = store.items
    assert (item.product_key, item.quantity, item.origin) == ("milk", 1000, "staple")


def test_a_staple_already_on_the_list_is_not_added_twice(store):
    milk = store.product("milk", staple=True)
    store.add_item(None, product=milk, quantity=2, origin="manual")
    stored = store.stored(milk)

    lifecycle.remove(FakeSession(), stored.id)

    assert [(i.origin, i.quantity) for i in store.items] == [("manual", 2)]


def test_expired_items_leave_storage_and_staples_come_back(store):
    yesterday = _today() - timedelta(days=1)
    milk = store.product("milk", staple=True)
    bread = store.product("bread")
    expired_milk = store.stored(milk, expires_on=yesterday)
    expired_bread = store.stored(bread, expires_on=yesterday)
    fresh = store.stored(bread, expires_on=_today())

    assert lifecycle.purge_expired(FakeSession()) == 2

    assert expired_milk.removed_reason == expired_bread.removed_reason == "expired"
    assert fresh.removed_at is None
    assert [i.product_key for i in store.items] == ["milk"]


def test_a_new_expiry_date_becomes_the_products_shelf_life(store):
    milk = store.product("milk", shelf_life_days=10)
    added = datetime.now(timezone.utc)
    stored = store.stored(milk, added_at=added)

    lifecycle.set_expiry(FakeSession(), stored.id, to_user_tz(added).date() + timedelta(days=4))

    assert milk.shelf_life_days == 4


@pytest.mark.asyncio
async def test_typing_a_listed_product_again_does_not_duplicate_it(store, monkeypatch):
    rice = store.product("rice", unit="g", pack_quantity=1000)

    async def resolve(session, name):
        return rice

    monkeypatch.setattr(lifecycle, "resolve", resolve)
    session = FakeSession()

    await lifecycle.add_entry(session, "rice")
    await lifecycle.add_entry(session, "rice")
    assert [i.quantity for i in store.items] == [1000]

    await lifecycle.add_entry(session, "500 g rice")
    assert [i.quantity for i in store.items] == [1500]


@pytest.mark.asyncio
async def test_typing_a_staple_entry_makes_it_manual(store, monkeypatch):
    milk = store.product("milk", staple=True)
    item = store.add_item(None, product=milk, quantity=1, origin="staple")

    async def resolve(session, name):
        return milk

    monkeypatch.setattr(lifecycle, "resolve", resolve)

    await lifecycle.add_entry(FakeSession(), "milk")

    assert item.origin == "manual"


@pytest.mark.asyncio
async def test_spellings_of_one_product_share_its_cache_row(monkeypatch):
    rows: dict[str, SimpleNamespace] = {}
    lookups: list[str] = []

    def find_product(session, key):
        return next((r for r in rows.values() if key == r.key or key in r.aliases), None)

    def add_product(session, *, key, alias, **facts):
        rows[key] = SimpleNamespace(key=key, aliases=[alias] if alias != key else [], **facts)
        return rows[key]

    async def lookup(text):
        lookups.append(text)
        return SimpleNamespace(
            name="Tomatoes", category="produce", unit="piece",
            pack_quantity=4, portion=1, shelf_life_days=7,
        )

    monkeypatch.setattr(grocery_store, "find_product", find_product)
    monkeypatch.setattr(grocery_store, "get_product", lambda session, key: rows.get(key))
    monkeypatch.setattr(grocery_store, "add_product", add_product)
    monkeypatch.setattr(products, "lookup", lookup)

    first = await products.resolve(None, "tomato")
    second = await products.resolve(None, "Tomaten")
    again = await products.resolve(None, "TOMATO")

    assert first is second is again
    assert first.key == "tomatoes"
    assert first.aliases == ["tomato", "tomaten"]
    assert lookups == ["tomato", "Tomaten"]
