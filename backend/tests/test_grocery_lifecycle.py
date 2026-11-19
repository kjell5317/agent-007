"""Shopping list → storage → used up or expired, against an in-memory store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.clients import grocery as grocery_store
from app.db.schemas.grocery import GroceryItemUpdate, StorageItemUpdate
from app.services.grocery import lifecycle, products, recheck, shopping
from app.timezones import to_user_tz
from grocery_fakes import FakeSession, patch_store


@pytest.fixture
def store(monkeypatch):
    return patch_store(monkeypatch)


@pytest.fixture(autouse=True)
def handoffs(monkeypatch):
    """What lifecycle hands on: storage changes for the re-check, shop-by
    days for the shopping task, and whether the list ran empty."""
    seen = SimpleNamespace(changes=[], shop_by=[], closes=0)

    async def ensure_task(session, shop_by):
        seen.shop_by.append(shop_by)

    async def close_if_done(session):
        seen.closes += 1

    monkeypatch.setattr(recheck, "note", seen.changes.append)
    monkeypatch.setattr(shopping, "ensure_task", ensure_task)
    monkeypatch.setattr(shopping, "close_if_done", close_if_done)
    return seen


def _today():
    return to_user_tz(datetime.now(timezone.utc)).date()


@pytest.mark.asyncio
async def test_cross_off_moves_the_item_into_storage(store):
    rice = store.product("rice", unit="g", pack_quantity=1000, portion=150, shelf_life_days=365)
    item = store.add_item(None, product=rice, quantity=500, origin="manual")

    await lifecycle.cross_off(FakeSession(), item.id)

    assert item.bought_at is not None
    [stored] = store.storage
    assert stored.grocery_item_id == item.id
    assert stored.quantity == 500
    assert stored.expires_on == _today() + timedelta(days=365)


@pytest.mark.asyncio
async def test_uncross_takes_it_back_out_of_storage(store):
    rice = store.product("rice")
    item = store.add_item(None, product=rice, quantity=1, origin="manual")
    await lifecycle.cross_off(FakeSession(), item.id)

    lifecycle.uncross(FakeSession(), item.id)

    assert item.bought_at is None
    assert store.storage == []


@pytest.mark.asyncio
async def test_portions_count_down_until_used_up(store):
    rice = store.product("rice", unit="g", pack_quantity=1000, portion=400)
    stored = store.stored(rice)

    await lifecycle.use_portion(FakeSession(), stored.id)
    await lifecycle.use_portion(FakeSession(), stored.id)
    assert stored.quantity == pytest.approx(200)
    assert stored.removed_at is None

    await lifecycle.use_portion(FakeSession(), stored.id)
    assert stored.removed_reason == "used_up"
    assert store.items == []  # not a staple


@pytest.mark.asyncio
async def test_a_staple_goes_back_on_the_list_once_the_last_one_is_used(store):
    milk = store.product("milk", unit="ml", pack_quantity=1000, portion=1000, staple=True)
    first, second = store.stored(milk), store.stored(milk)

    await lifecycle.use_portion(FakeSession(), first.id)
    assert store.items == []  # another carton is still at home

    await lifecycle.remove(FakeSession(), second.id)
    [item] = store.items
    assert (item.product_key, item.quantity, item.origin) == ("milk", 1000, "staple")


@pytest.mark.asyncio
async def test_a_staple_already_on_the_list_is_not_added_twice(store):
    milk = store.product("milk", staple=True)
    store.add_item(None, product=milk, quantity=2, origin="manual")
    stored = store.stored(milk)

    await lifecycle.remove(FakeSession(), stored.id)

    assert [(i.origin, i.quantity) for i in store.items] == [("manual", 2)]


@pytest.mark.asyncio
async def test_expired_items_leave_storage_and_staples_come_back(store):
    yesterday = _today() - timedelta(days=1)
    milk = store.product("milk", staple=True)
    bread = store.product("bread")
    expired_milk = store.stored(milk, expires_on=yesterday)
    expired_bread = store.stored(bread, expires_on=yesterday)
    fresh = store.stored(bread, expires_on=_today())

    assert await lifecycle.purge_expired(FakeSession()) == 2

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
async def test_a_new_amount_left_is_the_bars_new_full(store):
    rice = store.product("rice", unit="g", pack_quantity=1000, portion=150)
    stored = store.stored(rice)
    await lifecycle.use_portion(FakeSession(), stored.id)
    assert (stored.quantity, stored.start_quantity) == (850, 1000)

    await lifecycle.update_storage(FakeSession(), stored.id, StorageItemUpdate(quantity=400))
    assert (stored.quantity, stored.start_quantity) == (400, 400)

    # A new portion alone leaves the bar's full mark where it was.
    await lifecycle.use_portion(FakeSession(), stored.id)
    await lifecycle.update_storage(FakeSession(), stored.id, StorageItemUpdate(portion=100))
    assert (stored.quantity, stored.start_quantity, stored.portion) == (250, 400, 100)


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


@pytest.mark.asyncio
async def test_using_or_removing_by_hand_starts_the_recheck(store, handoffs):
    rice = store.product("rice", name="Reis", unit="g", pack_quantity=1000, portion=150)
    bread = store.product("bread", name="Brot", unit="piece", pack_quantity=1, portion=1)
    item = store.add_item(None, product=rice, quantity=1000, origin="manual")
    used, removed = store.stored(rice), store.stored(bread)
    store.stored(bread, expires_on=_today() - timedelta(days=1))

    await lifecycle.cross_off(FakeSession(), item.id)
    await lifecycle.purge_expired(FakeSession())
    assert handoffs.changes == []  # neither counts

    await lifecycle.use_portion(FakeSession(), used.id)
    await lifecycle.remove(FakeSession(), removed.id)
    assert handoffs.changes == [
        "Used 150 g of Reis, 850 g left",
        "Took Brot out of storage with 1 piece left",
    ]


@pytest.mark.asyncio
async def test_a_staple_running_out_means_shopping_tomorrow(store, handoffs):
    milk = store.product("milk", unit="ml", pack_quantity=1000, portion=1000, staple=True)
    bread = store.product("bread", portion=1)
    await lifecycle.use_portion(FakeSession(), store.stored(bread).id)
    assert handoffs.shop_by == []  # not starred

    await lifecycle.use_portion(FakeSession(), store.stored(milk).id)
    assert handoffs.shop_by == [_today() + timedelta(days=1)]

    store.stored(milk, expires_on=_today() - timedelta(days=1))
    store.items.clear()
    await lifecycle.purge_expired(FakeSession())
    assert handoffs.shop_by[-1] == _today() + timedelta(days=1)


@pytest.mark.asyncio
async def test_every_cross_off_checks_whether_the_trip_is_done(store, handoffs):
    rice = store.product("rice")
    item = store.add_item(None, product=rice, quantity=1, origin="manual")

    await lifecycle.cross_off(FakeSession(), item.id)

    assert handoffs.closes == 1
