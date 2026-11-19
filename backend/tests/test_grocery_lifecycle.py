"""Shopping list → storage → used up or expired, against an in-memory store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.clients import grocery as grocery_store
from app.db.schemas.grocery import GroceryItemUpdate, StorageItemUpdate
from app.services.grocery import lifecycle, products
from app.timezones import to_user_tz
from grocery_fakes import FakeSession, patch_store


@pytest.fixture
def store(monkeypatch):
    return patch_store(monkeypatch)


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


@pytest.mark.asyncio
async def test_a_new_expiry_date_becomes_the_products_shelf_life(store):
    milk = store.product("milk", shelf_life_days=10)
    added = datetime.now(timezone.utc)
    stored = store.stored(milk, added_at=added)
    expires_on = to_user_tz(added).date() + timedelta(days=4)

    await lifecycle.update_storage(FakeSession(), stored.id, StorageItemUpdate(expires_on=expires_on))

    assert stored.expires_on == expires_on
    assert milk.shelf_life_days == 4


@pytest.mark.asyncio
async def test_a_new_unit_and_portion_restart_the_bar_and_teach_the_cache(store):
    juice = store.product("orangensaft", unit="bottle", pack_quantity=1, portion=0.25)
    stored = store.stored(juice)

    await lifecycle.update_storage(
        FakeSession(), stored.id, StorageItemUpdate(quantity=1000, unit="ml", portion=200)
    )

    assert (stored.quantity, stored.start_quantity, stored.unit, stored.portion) == (
        1000, 1000, "ml", 200,
    )
    assert (juice.unit, juice.pack_quantity, juice.portion) == ("ml", 1000, 200)


@pytest.mark.asyncio
async def test_a_unit_change_needs_the_portion_in_that_unit(store):
    juice = store.product("orangensaft", unit="bottle", portion=0.25)
    stored = store.stored(juice)

    with pytest.raises(ValueError):
        await lifecycle.update_storage(FakeSession(), stored.id, StorageItemUpdate(unit="ml"))


@pytest.mark.asyncio
async def test_using_less_than_bought_keeps_the_bar_full_length(store):
    rice = store.product("rice", unit="g", pack_quantity=1000, portion=150)
    stored = store.stored(rice)

    await lifecycle.update_storage(FakeSession(), stored.id, StorageItemUpdate(quantity=400))

    assert (stored.quantity, stored.start_quantity) == (400, 1000)


@pytest.mark.asyncio
async def test_renaming_a_misread_word_gives_it_its_own_product(store, monkeypatch):
    eggs = store.product("eggs", name="Eggs", category="dairy", unit="piece", aliases=["eis"])
    item = store.add_item(None, product=eggs, quantity=10, origin="manual")

    async def create_named(session, name):
        return store.product("eis", name=name, category="frozen", unit="pack", pack_quantity=1)

    monkeypatch.setattr(lifecycle, "create_named", create_named)

    await lifecycle.update_item(FakeSession(), item.id, GroceryItemUpdate(name="Eis"))

    assert eggs.aliases == []  # "eis" no longer resolves to eggs
    assert (item.product_key, item.name, item.category, item.unit, item.quantity) == (
        "eis", "Eis", "frozen", "pack", 1,
    )


@pytest.mark.asyncio
async def test_renaming_to_a_known_product_moves_the_entry_there(store, monkeypatch):
    garlic = store.product("knoblauch", name="Knoblauch", unit="piece")
    bulbs = store.product("knoblauchknolle", name="Knoblauchknolle", unit="piece", portion=0.25)
    item = store.add_item(None, product=garlic, quantity=3, origin="manual")

    async def create_named(session, name):
        raise AssertionError("no lookup for a cached product")

    monkeypatch.setattr(lifecycle, "create_named", create_named)

    await lifecycle.update_item(FakeSession(), item.id, GroceryItemUpdate(name="Knoblauchknolle"))

    assert (item.product, item.quantity, item.portion) == (bulbs, 3, 0.25)


@pytest.mark.asyncio
async def test_renaming_only_the_spelling_keeps_the_product(store):
    eggs = store.product("eggs", name="eggs")
    item = store.add_item(None, product=eggs, quantity=10, origin="manual")

    await lifecycle.update_item(
        FakeSession(), item.id, GroceryItemUpdate(name="Eggs", category="dairy")
    )

    assert (item.product, item.name, eggs.name, eggs.category) == (eggs, "Eggs", "Eggs", "dairy")


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
