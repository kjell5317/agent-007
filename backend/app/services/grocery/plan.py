"""The weekly plan: recipes for the configured number of meals and the
shopping-list entries they still need. One plan runs at a time, in the
background; the browser hears over SSE when it starts and ends.

A replan replaces this week's recipes and every open entry the agent added.
Manual and starred entries are never changed or removed."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.agent.grocery.planner import (
    Listed,
    Plan,
    PlanError,
    PlannerContext,
    ShoppingNeed,
    Stock,
)
from app.agent.grocery.planner import plan as run_planner
from app.db import SessionLocal
from app.db.clients import grocery as grocery_store
from app.db.models.grocery import GroceryProduct, GrocerySettings
from app.db.schemas.grocery import GrocerySettingsUpdate, RecipeRead, RecipesState
from app.events import publish_grocery
from app.services.grocery.normalize import product_key, purchasable, week_start
from app.services.grocery.usage import Purchase, often_expired, weekly_usage, weeks_observed
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

USAGE_WEEKS = 4
EXPIRED_WINDOW = timedelta(weeks=8)
RATED_LIMIT = 10
ARCHIVE_LIMIT = 60

_planning = False
_error: str | None = None
# Held so the running plan isn't garbage-collected mid-flight.
_task: asyncio.Task | None = None


def request_plan() -> bool:
    """Replan this week in the background; False when a plan is already running."""
    global _planning, _task
    if _planning:
        return False
    _planning = True
    _task = asyncio.create_task(_run(), name="grocery-plan")
    publish_grocery()
    return True


def needs_plan(session: Session) -> bool:
    if _planning:
        return False
    settings = grocery_store.settings(session)
    week = week_start(_today())
    return settings.recipes_per_week > 0 and not grocery_store.recipes_for_week(session, week)


async def _run() -> None:
    global _planning, _error
    try:
        with SessionLocal() as session:
            await plan_week(session)
        _error = None
    except PlanError as exc:
        log.warning("grocery plan failed: %s", exc)
        _error = str(exc)
    except Exception:  # noqa: BLE001 — the UI shows the failure; the next trigger retries
        log.exception("grocery plan failed")
        _error = "Planning failed"
    finally:
        _planning = False
        publish_grocery()


async def plan_week(session: Session) -> None:
    now = datetime.now(timezone.utc)
    week = week_start(to_user_tz(now).date())
    settings = grocery_store.settings(session)
    servings = settings.servings
    plan = Plan(recipes=[], shopping=[])
    if settings.recipes_per_week > 0:
        context = _context(session, now, settings)
        # Don't hold a transaction open across the LLM call.
        session.commit()
        plan = await run_planner(context)
    apply_plan(session, week, servings, plan)
    session.commit()
    log.info(
        "grocery plan done · week=%s recipes=%d shopping=%d",
        week, len(plan.recipes), len(plan.shopping),
    )


def apply_plan(session: Session, week: date, servings: int, plan: Plan) -> None:
    for recipe in grocery_store.recipes_for_week(session, week):
        grocery_store.delete(session, recipe)
    for recipe in plan.recipes:
        grocery_store.add_recipe(
            session,
            week_start=week,
            title=recipe.title,
            servings=servings,
            ingredients=recipe.ingredients,
            steps=recipe.steps,
        )
    for item in grocery_store.open_items(session):
        if item.origin == "agent":
            grocery_store.delete(session, item)
    for need in plan.shopping:
        key = product_key(need.name)
        if not key:
            continue
        product = grocery_store.find_product(session, key) or _new_product(session, key, need)
        quantity = _list_amount(need, product)
        listed = grocery_store.open_item_for(session, product.key)
        if listed is None:
            grocery_store.add_item(
                session,
                product=product,
                quantity=quantity,
                origin="agent",
                recipe_titles=list(need.recipes),
            )
        elif listed.origin == "agent":
            listed.quantity += quantity
            listed.recipe_titles = list(dict.fromkeys([*listed.recipe_titles, *need.recipes]))


def _new_product(session: Session, key: str, need: ShoppingNeed) -> GroceryProduct:
    return grocery_store.add_product(
        session,
        key=key,
        alias=None,
        name=need.name,
        category=need.category,
        unit=need.unit,
        pack_quantity=need.quantity,
        portion=need.portion,
        shelf_life_days=need.shelf_life_days,
    )


def _list_amount(need: ShoppingNeed, product: GroceryProduct) -> float:
    # The cached product may count in another unit than the planner did.
    if need.unit != product.unit:
        return product.pack_quantity
    return purchasable(need.quantity, unit=product.unit, pack_quantity=product.pack_quantity)


def _context(session: Session, now: datetime, settings: GrocerySettings) -> PlannerContext:
    today = to_user_tz(now).date()
    bought = grocery_store.bought_since(session, now - timedelta(weeks=USAGE_WEEKS))
    first = min((item.bought_at for item in bought), default=None)
    return PlannerContext(
        today=today,
        week_start=week_start(today),
        diet=settings.diet,
        recipes=settings.recipes_per_week,
        servings=settings.servings,
        storage=[
            Stock(s.name, s.quantity, s.unit, s.expires_on)
            for s in grocery_store.active_storage(session)
        ],
        listed=[
            Listed(i.name, i.quantity, i.unit, i.origin)
            for i in grocery_store.open_items(session)
            if i.origin != "agent"
        ],
        usual=weekly_usage(
            [Purchase(i.product_key, i.name, i.quantity, i.unit) for i in bought],
            weeks=weeks_observed(first, now, cap=USAGE_WEEKS),
        ),
        expired=often_expired(
            [s.name for s in grocery_store.expired_since(session, now - EXPIRED_WINDOW)]
        ),
        liked=[r.title for r in grocery_store.rated_recipes(session, 1, limit=RATED_LIMIT)],
        disliked=[r.title for r in grocery_store.rated_recipes(session, -1, limit=RATED_LIMIT)],
    )


def recipes_state(session: Session) -> RecipesState:
    week = week_start(_today())
    return RecipesState(
        week_start=week,
        recipes_per_week=grocery_store.settings(session).recipes_per_week,
        planning=_planning,
        error=_error,
        this_week=[
            RecipeRead.model_validate(r) for r in grocery_store.recipes_for_week(session, week)
        ],
        archive=[
            RecipeRead.model_validate(r)
            for r in grocery_store.recipes_before(session, week, limit=ARCHIVE_LIMIT)
        ],
    )


def rate_recipe(session: Session, recipe_id: uuid.UUID, rating: int) -> None:
    recipe = grocery_store.get_recipe(session, recipe_id)
    if recipe is None:
        raise LookupError("Recipe not found")
    recipe.rating = rating or None
    session.commit()


def update_settings(session: Session, edit: GrocerySettingsUpdate) -> GrocerySettings:
    """Saves the settings; a change to what gets cooked replans this week."""
    row = grocery_store.settings(session)
    before = (row.diet, row.recipes_per_week, row.servings)
    if edit.diet is not None:
        row.diet = edit.diet
    if edit.recipes_per_week is not None:
        row.recipes_per_week = edit.recipes_per_week
    if edit.servings is not None:
        row.servings = edit.servings
    if edit.store_location is not None:
        row.store_location = edit.store_location.strip() or None
    session.commit()
    if (row.diet, row.recipes_per_week, row.servings) != before:
        request_plan()
    return row


def _today() -> date:
    return to_user_tz(datetime.now(timezone.utc)).date()
