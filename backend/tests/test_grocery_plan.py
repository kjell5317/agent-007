"""The weekly plan: what the planner sees, what it may return, and how its
answer lands on the shopping list without touching the user's own entries."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.agent.grocery.planner import (
    Listed,
    Plan,
    PlanError,
    PlannedRecipe,
    PlannerContext,
    ShoppingNeed,
    Stock,
    parse_plan,
    planner_message,
)
from app.db.clients import grocery as grocery_store
from app.db.schemas.grocery import GrocerySettingsUpdate
from app.services.grocery import plan
from app.services.grocery.normalize import purchasable, week_start
from app.services.grocery.usage import Purchase, often_expired, weekly_usage, weeks_observed
from grocery_fakes import FakeSession, patch_store


@pytest.fixture
def store(monkeypatch):
    return patch_store(monkeypatch)

WEEK = date(2026, 11, 16)


def _need(name, quantity, unit="g", **facts):
    return ShoppingNeed(
        name=name, quantity=quantity, unit=unit, portion=facts.get("portion", 1),
        category=facts.get("category", "other"), shelf_life_days=facts.get("shelf_life_days", 7),
    )


def _recipe(title):
    return PlannedRecipe(title, [{"name": "Rice", "amount": "150 g"}], ["Cook it."])


def test_a_replan_replaces_recipes_and_agent_entries_only(store):
    rice = store.product("rice", name="Rice", unit="g", pack_quantity=1000)
    milk = store.product("milk", name="Milk", unit="ml", pack_quantity=1000, staple=True)
    garlic = store.product("knoblauch", name="Knoblauch", unit="piece")
    store.add_recipe(None, week_start=WEEK, title="Old curry")
    kept = store.add_recipe(None, week_start=WEEK - timedelta(days=7), title="Last week")
    manual = store.add_item(None, product=garlic, quantity=3, origin="manual")
    staple = store.add_item(None, product=milk, quantity=1000, origin="staple")
    store.add_item(None, product=rice, quantity=1000, origin="agent")

    plan.apply_plan(FakeSession(), WEEK, 2, Plan(
        recipes=[_recipe("Risotto"), _recipe("Dal")],
        shopping=[
            _need("Rice", 300),                      # rounds up to the 1 kg bag
            _need("Knoblauch", 6, unit="piece"),     # the user's entry stays as is
            _need("Milk", 500, unit="ml"),           # so does the starred one
            _need("Linsen", 500, category="dry_goods"),
        ],
    ))

    assert sorted(r.title for r in store.recipes) == ["Dal", "Last week", "Risotto"]
    assert kept in store.recipes
    assert all(r.servings == 2 for r in store.recipes if r.week_start == WEEK)
    listed = {i.name: (i.quantity, i.origin) for i in store.items}
    assert listed == {
        "Knoblauch": (3, "manual"),
        "Milk": (1000, "staple"),
        "Rice": (1000, "agent"),
        "Linsen": (500, "agent"),
    }
    assert manual in store.items and staple in store.items
    assert store.products["linsen"].category == "dry_goods"


def test_a_planned_amount_in_another_unit_falls_back_to_the_usual_pack(store):
    store.product("eggs", name="Eggs", unit="piece", pack_quantity=10)

    plan.apply_plan(FakeSession(), WEEK, 1, Plan(recipes=[], shopping=[_need("Eggs", 1, unit="pack")]))

    assert [(i.name, i.quantity, i.unit) for i in store.items] == [("Eggs", 10, "piece")]


def test_the_same_product_planned_twice_adds_up(store):
    plan.apply_plan(FakeSession(), WEEK, 1, Plan(recipes=[], shopping=[
        _need("Tomatoes", 2, unit="piece"), _need("tomatoes", 3, unit="piece"),
    ]))

    assert [(i.name, i.quantity) for i in store.items] == [("Tomatoes", 5)]


@pytest.mark.parametrize(
    ("changed", "replans"),
    [({"servings": 2}, True), ({"diet": "vegan"}, True), ({"store_location": "Rewe"}, False)],
)
def test_only_cooking_settings_replan(monkeypatch, changed, replans):
    row = SimpleNamespace(diet="omnivore", recipes_per_week=3, servings=1, store_location=None)
    monkeypatch.setattr(grocery_store, "settings", lambda session: row)
    calls = []
    monkeypatch.setattr(plan, "request_plan", lambda: calls.append(True))

    plan.update_settings(FakeSession(), GrocerySettingsUpdate(**changed))

    assert bool(calls) is replans


def test_parse_plan_keeps_usable_recipes_and_entries():
    parsed = parse_plan({
        "recipes": [
            {"title": "Dal", "ingredients": [{"name": "Linsen", "amount": "200 g"}], "steps": ["Kochen"]},
            {"title": "No steps", "ingredients": [{"name": "Reis", "amount": "1"}], "steps": []},
            {"title": "Extra", "ingredients": [{"name": "Reis", "amount": "1"}], "steps": ["x"]},
        ],
        "shopping": [
            {"name": "Linsen", "quantity": 500, "unit": "g", "portion": 100,
             "category": "dry_goods", "shelf_life_days": 365},
            {"name": "Mystery", "quantity": 1, "unit": "crate", "portion": 1,
             "category": "other", "shelf_life_days": 3},
        ],
    }, recipes=1)

    assert [r.title for r in parsed.recipes] == ["Dal"]
    assert [(n.name, n.quantity, n.unit) for n in parsed.shopping] == [("Linsen", 500, "g")]
    with pytest.raises(PlanError):
        parse_plan({"recipes": [], "shopping": []}, recipes=3)
    assert parse_plan({"recipes": [], "shopping": []}, recipes=0) == Plan([], [])


def test_the_planner_sees_storage_list_habits_and_ratings():
    message = planner_message(PlannerContext(
        today=date(2026, 11, 18), week_start=WEEK, diet="vegetarian", recipes=3, servings=2,
        storage=[Stock("Milch", 800, "ml", date(2026, 11, 25))],
        listed=[Listed("Knoblauch", 3, "piece", "manual")],
        usual=weekly_usage([Purchase("milch", "Milch", 2000, "ml")], weeks=1),
        expired=["Spinat"], liked=["Dal"], disliked=["Tofu-Curry"],
    ))

    assert "Wednesday, 2026-11-18" in message
    assert "Diet: vegetarian" in message
    assert "Recipes: 3, each for 2 servings" in message
    assert "- Milch: 800 ml, until 2026-11-25" in message
    assert "- Knoblauch: 3 piece (added by the user)" in message
    assert "- Milch: 2000 ml" in message
    assert "Often expired before it was used: Spinat" in message
    assert "Liked recipes: Dal" in message
    assert "Disliked recipes: Tofu-Curry" in message


def test_usual_amounts_average_over_the_weeks_seen():
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    purchases = [
        Purchase("milk", "Milk", 1000, "ml"),
        Purchase("milk", "Milk", 1000, "ml"),
        Purchase("rice", "Rice", 1000, "g"),
        Purchase("milk", "Milk", 1, "bottle"),
    ]
    usage = weekly_usage(purchases, weeks=2)
    assert [(u.name, u.quantity, u.unit) for u in usage] == [
        ("Milk", 1000, "ml"), ("Milk", 0.5, "bottle"), ("Rice", 500, "g"),
    ]
    assert weeks_observed(None, now, cap=4) == 1
    assert weeks_observed(now - timedelta(days=10), now, cap=4) == 2
    assert weeks_observed(now - timedelta(days=90), now, cap=4) == 4
    assert often_expired(["Spinat", "Brot", "Spinat"]) == ["Spinat", "Brot"]


def test_amounts_round_up_to_what_a_store_sells():
    assert purchasable(300, unit="g", pack_quantity=1000) == 1000
    assert purchasable(1000, unit="g", pack_quantity=1000) == 1000
    assert purchasable(1200, unit="ml", pack_quantity=500) == 1500
    assert purchasable(2.5, unit="piece", pack_quantity=4) == 3
    assert purchasable(0.5, unit="pack", pack_quantity=1) == 1


def test_weeks_start_on_monday():
    assert week_start(date(2026, 11, 18)) == WEEK
    assert week_start(WEEK) == WEEK
    assert week_start(date(2026, 11, 22)) == WEEK
