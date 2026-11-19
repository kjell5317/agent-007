"""The shopping task and the quick re-check: when a trip gets planned,
brought forward or closed, and what the re-check may change on the list."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agent.grocery.planner import (
    Adjustment,
    Entry,
    PlannedRecipe,
    RecheckContext,
    ShoppingNeed,
    parse_adjustment,
    recheck_message,
)
from app.db.clients import grocery as grocery_store
from app.db.clients import tasks as tasks_store
from app.services.grocery import plan, recheck, shopping
from app.timezones import user_tz
from grocery_fakes import FakeSession, patch_store

TODAY = date(2026, 11, 19)


class TaskSession(FakeSession):
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


@pytest.fixture
def tasks(monkeypatch):
    """Tasks as the shopping service sees them, plus what it asked for."""
    world = SimpleNamespace(
        settings=SimpleNamespace(
            store_location="Rewe, Lindwurmstr.", shopping_task_id=None, shop_by_time=time(18),
        ),
        rows={}, statuses={}, scheduled=[], updates=[], closed=[],
    )

    def create(session, payload):
        row = SimpleNamespace(id=uuid.uuid4(), **payload.model_dump())
        world.rows[row.id] = row
        return row

    async def schedule_task(session, task):
        world.scheduled.append(task.id)

    async def update_task(session, task_id, fields):
        world.updates.append((task_id, fields))

    async def close_task(session, task_id):
        world.closed.append(task_id)

    monkeypatch.setattr(grocery_store, "settings", lambda session: world.settings)
    monkeypatch.setattr(tasks_store, "create", create)
    monkeypatch.setattr(tasks_store, "get", lambda session, task_id: world.rows.get(task_id))
    monkeypatch.setattr(
        tasks_store, "latest_status_for",
        lambda session, ids: {i: world.statuses.get(i, "open") for i in ids},
    )
    monkeypatch.setattr(shopping, "schedule_task", schedule_task)
    monkeypatch.setattr(shopping, "update_task", update_task)
    monkeypatch.setattr(shopping, "close_task", close_task)
    monkeypatch.setattr(shopping, "publish_task", lambda session, task_id: None)
    return world


def _at(day, hour):
    return datetime(day.year, day.month, day.day, hour, tzinfo=user_tz())


@pytest.mark.asyncio
async def test_running_out_creates_one_shopping_task_at_the_store(tasks):
    session = TaskSession()
    shop_by = date.today() + timedelta(days=2)

    await shopping.ensure_task(session, shop_by)

    [task] = tasks.rows.values()
    assert task.title == "Grocery shopping"
    assert (task.due_date, task.estimation, task.location) == (
        _at(shop_by, 18), 45, "Rewe, Lindwurmstr.",
    )
    assert task.link.endswith("/#grocery")
    [anchor] = session.added
    assert (anchor.source, anchor.status, anchor.task_id) == ("grocery", "open", task.id)
    assert tasks.settings.shopping_task_id == task.id
    assert tasks.scheduled == [task.id]


@pytest.mark.asyncio
async def test_an_open_task_only_ever_moves_earlier(tasks):
    session = TaskSession()
    later = date.today() + timedelta(days=4)
    await shopping.ensure_task(session, later)
    [task] = tasks.rows.values()

    await shopping.ensure_task(session, later + timedelta(days=2))
    assert tasks.updates == []

    sooner = date.today() + timedelta(days=1)
    await shopping.ensure_task(session, sooner)
    assert tasks.updates == [(task.id, {"due_date": _at(sooner, 18)})]
    assert len(tasks.rows) == 1


@pytest.mark.asyncio
async def test_a_closed_trip_makes_room_for_a_new_one(tasks):
    session = TaskSession()
    await shopping.ensure_task(session, date.today() + timedelta(days=1))
    [first] = tasks.rows.values()
    tasks.statuses[first.id] = "closed"

    await shopping.ensure_task(session, date.today() + timedelta(days=1))

    assert len(tasks.rows) == 2
    assert tasks.settings.shopping_task_id != first.id


def test_a_shop_by_time_already_past_still_leaves_an_hour():
    now = datetime.now(timezone.utc)
    yesterday = date.today() - timedelta(days=1)
    assert shopping.due_for(yesterday, time(18), now) == now + timedelta(hours=1)


@pytest.mark.asyncio
async def test_the_stores_shop_by_time_sets_the_due_time(tasks):
    tasks.settings.shop_by_time = time(19, 30)
    shop_by = date.today() + timedelta(days=2)

    await shopping.ensure_task(TaskSession(), shop_by)

    [task] = tasks.rows.values()
    assert task.due_date == datetime.combine(shop_by, time(19, 30), tzinfo=user_tz())


@pytest.mark.asyncio
async def test_the_last_cross_off_closes_the_trip(tasks, monkeypatch):
    open_items = [SimpleNamespace()]
    monkeypatch.setattr(grocery_store, "open_items", lambda session: open_items)
    session = TaskSession()
    await shopping.close_if_done(session)  # nothing to close yet
    await shopping.ensure_task(session, date.today() + timedelta(days=1))
    [task] = tasks.rows.values()

    await shopping.close_if_done(session)
    assert tasks.closed == []

    open_items.clear()
    await shopping.close_if_done(session)
    assert tasks.closed == [task.id]


@pytest.mark.asyncio
async def test_new_store_settings_move_the_open_trip(tasks):
    session = TaskSession()
    shop_by = date.today() + timedelta(days=1)
    await shopping.ensure_task(session, shop_by)
    [task] = tasks.rows.values()

    await shopping.sync_store(session)
    assert tasks.updates == []  # nothing changed

    tasks.settings.store_location = "Edeka"
    tasks.settings.shop_by_time = time(12)
    await shopping.sync_store(session)
    assert tasks.updates == [(task.id, {"location": "Edeka", "due_date": _at(shop_by, 12)})]


def _need(name, quantity, unit="g", recipes=()):
    return ShoppingNeed(name, quantity, unit, 1, "other", 7, tuple(recipes))


def test_the_recheck_only_touches_the_agents_own_entries(monkeypatch):
    store = patch_store(monkeypatch)
    rice = store.product("rice", name="Rice", unit="g", pack_quantity=1000)
    garlic = store.product("knoblauch", name="Knoblauch", unit="piece")
    milk = store.product("milk", name="Milk", unit="ml", staple=True)
    lentils = store.product("linsen", name="Linsen", unit="g", pack_quantity=500)
    manual = store.add_item(None, product=garlic, quantity=3, origin="manual")
    starred = store.add_item(None, product=milk, quantity=1000, origin="staple")
    own_rice = store.add_item(None, product=rice, quantity=1000, origin="agent")
    own_lentils = store.add_item(None, product=lentils, quantity=500, origin="agent")

    recheck.apply_adjustment(FakeSession(), Adjustment(
        add=[_need("Tomatoes", 4, unit="piece", recipes=["Dal"])],
        update={str(own_rice.id): 1200, str(manual.id): 10},
        drop=[str(own_lentils.id), str(starred.id)],
        shop_by=None,
    ))

    listed = {i.name: (i.quantity, i.origin) for i in store.items}
    assert listed == {
        "Knoblauch": (3, "manual"),
        "Milk": (1000, "staple"),
        "Rice": (2000, "agent"),  # rounded up to whole 1 kg bags
        "Tomatoes": (4, "agent"),
    }


def test_parse_adjustment_keeps_what_it_can_use():
    adjustment = parse_adjustment({
        "add": [
            {"name": "Tomaten", "quantity": 4, "unit": "piece", "portion": 1,
             "category": "produce", "shelf_life_days": 7, "recipes": ["Dal", "Pizza"]},
        ],
        "update": [{"id": "a", "quantity": 2}, {"id": "b", "quantity": 0}, {"id": "c", "quantity": "x"}],
        "drop": ["d", " ", 5],
        "shop_by": "2026-11-17",
    }, today=TODAY, titles={"Dal"})

    assert [(n.name, n.recipes) for n in adjustment.add] == [("Tomaten", ("Dal",))]
    assert adjustment.update == {"a": 2.0}
    assert adjustment.drop == ["d", "5"]
    assert adjustment.shop_by == TODAY  # a day already gone means now
    assert parse_adjustment({"shop_by": ""}, today=TODAY, titles=set()).shop_by is None


def test_the_recheck_names_only_its_own_entries_by_id():
    message = recheck_message(RecheckContext(
        today=TODAY,
        changes=["Used 200 ml of Milch, 800 ml left"],
        diet="vegetarian",
        servings=2,
        storage=[],
        entries=[
            Entry("a1", "Linsen", 500, "g", "agent", ["Dal"]),
            Entry("m1", "Knoblauch", 3, "piece", "manual", []),
        ],
        recipes=[PlannedRecipe("Dal", [{"name": "Linsen", "amount": "200 g"}], ["Kochen."])],
        usual=[],
    ))

    assert "- Used 200 ml of Milch, 800 ml left" in message
    assert "- Dal: Linsen 200 g" in message
    assert "- id=a1: Linsen: 500 g (yours, for Dal)" in message
    assert "- Knoblauch: 3 piece (added by the user)" in message
    assert "m1" not in message


@pytest.mark.asyncio
async def test_storage_changes_wait_for_a_quiet_spell_then_recheck_once(monkeypatch):
    runs = []

    async def run(session, changes):
        runs.append(changes)

    @contextmanager
    def session_local():
        yield FakeSession()

    monkeypatch.setattr(recheck, "QUIET_S", 0.05)
    monkeypatch.setattr(recheck, "recheck", run)
    monkeypatch.setattr(recheck, "SessionLocal", session_local)
    monkeypatch.setattr(recheck, "publish_grocery", lambda: None)
    monkeypatch.setattr(plan, "is_planning", lambda: False)

    recheck.note("Used 150 g of Reis")
    await asyncio.sleep(0.02)
    recheck.note("Took Brot out of storage")
    await asyncio.sleep(0.03)
    assert runs == []  # the second change restarted the wait

    await asyncio.sleep(0.06)
    assert runs == [["Used 150 g of Reis", "Took Brot out of storage"]]
