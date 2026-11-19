"""The weekly plan: recipes for the configured number of meals and the
shopping-list entries they still need. One plan runs at a time, in the
background; the browser hears over SSE when it starts and ends.

A replan replaces this week's recipes and every open entry the agent added.
Manual and starred entries are never changed or removed. When the plan says
the user has to shop by a day, the shopping task is created or brought
forward."""

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
from app.db.schemas.grocery import (
    GroceryPreferences,
    GrocerySettingsUpdate,
    RecipeRead,
    RecipesState,
    WeeklyAmountRead,
)
from app.events import publish_grocery
from app.services.grocery import shopping
from app.services.grocery.normalize import covered_by, product_key, purchasable, week_start
from app.services.grocery.usage import (
    Purchase,
    WeeklyAmount,
    often_expired,
    weekly_usage,
    weeks_observed,
)
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

# How much of the history goes into the plan's prompt; the settings page
# shows the same numbers.
USAGE_WEEKS = 4
USUAL_LIMIT = 25
EXPIRED_WEEKS = 8
EXPIRED_LIMIT = 10
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


def is_planning() -> bool:
    return _planning


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
    cooked = [r.title for r in grocery_store.recipes_for_week(session, week) if r.done_at]
    plan = Plan(recipes=[], shopping=[])
    # Cooked recipes count toward the week.
    remaining = max(0, settings.recipes_per_week - len(cooked))
    if remaining > 0:
        context = _context(session, now, settings, recipes=remaining, cooked=cooked)
        # Don't hold a transaction open across the LLM call.
        session.commit()
        plan = await run_planner(context)
    apply_plan(session, week, servings, plan)
    session.commit()
    if plan.shop_by is not None and grocery_store.open_items(session):
        await shopping.ensure_task(session, plan.shop_by)
    log.info(
        "grocery plan done · week=%s recipes=%d shopping=%d",
        week, len(plan.recipes), len(plan.shopping),
    )


def apply_plan(session: Session, week: date, servings: int, plan: Plan) -> None:
    for recipe in grocery_store.recipes_for_week(session, week):
        if recipe.done_at is None:
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
    add_needs(session, plan.shopping)


def add_needs(session: Session, needs: list[ShoppingNeed]) -> None:
    """New agent entries, merged into the agent's own entry for the same
    product; a product the user listed or starred is left as they have it, and
    what storage already covers for a recipe isn't bought again."""
    for need in needs:
        key = product_key(need.name)
        if not key:
            continue
        product = grocery_store.find_product(session, key)
        if product is not None and need.recipes and covered_by(
            need.needed,
            need.unit,
            [(s.quantity, s.unit) for s in grocery_store.stock_of(session, product.key)],
        ):
            continue
        product = product or _new_product(session, key, need)
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


def usual_amounts(session: Session, now: datetime) -> list[WeeklyAmount]:
    bought = grocery_store.bought_since(session, now - timedelta(weeks=USAGE_WEEKS))
    first = min((item.bought_at for item in bought), default=None)
    return weekly_usage(
        [Purchase(i.product_key, i.name, i.quantity, i.unit) for i in bought],
        weeks=weeks_observed(first, now, cap=USAGE_WEEKS),
        limit=USUAL_LIMIT,
    )


def often_expired_names(session: Session, now: datetime) -> list[str]:
    expired = grocery_store.expired_since(session, now - timedelta(weeks=EXPIRED_WEEKS))
    return often_expired([s.name for s in expired], limit=EXPIRED_LIMIT)


def _context(
    session: Session,
    now: datetime,
    settings: GrocerySettings,
    *,
    recipes: int,
    cooked: list[str],
) -> PlannerContext:
    today = to_user_tz(now).date()
    return PlannerContext(
        today=today,
        week_start=week_start(today),
        diet=settings.diet,
        recipes=recipes,
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
        usual=usual_amounts(session, now),
        expired=often_expired_names(session, now),
        liked=[r.title for r in grocery_store.rated_recipes(session, 1, limit=RATED_LIMIT)],
        disliked=[r.title for r in grocery_store.rated_recipes(session, -1, limit=RATED_LIMIT)],
        cooked=cooked,
    )


def preferences(session: Session) -> GroceryPreferences:
    now = datetime.now(timezone.utc)
    return GroceryPreferences(
        liked=[r.title for r in grocery_store.rated_recipes(session, 1, limit=RATED_LIMIT)],
        disliked=[r.title for r in grocery_store.rated_recipes(session, -1, limit=RATED_LIMIT)],
        usual=[
            WeeklyAmountRead(name=u.name, quantity=u.quantity, unit=u.unit)
            for u in usual_amounts(session, now)
        ],
        expired=often_expired_names(session, now),
        rated_limit=RATED_LIMIT,
        usual_limit=USUAL_LIMIT,
        usual_weeks=USAGE_WEEKS,
        expired_limit=EXPIRED_LIMIT,
        expired_weeks=EXPIRED_WEEKS,
    )


def recipes_state(session: Session) -> RecipesState:
    week = week_start(_today())
    return RecipesState(
        week_start=week,
        recipes_per_week=grocery_store.settings(session).recipes_per_week,
        planning=_planning,
        error=_error,
        this_week=[
            RecipeRead.model_validate(r)
            for r in grocery_store.recipes_for_week(session, week)
            if r.done_at is None
        ],
        archive=[
            RecipeRead.model_validate(r)
            for r in grocery_store.archived_recipes(session, week, limit=ARCHIVE_LIMIT)
        ],
    )


def set_done(session: Session, recipe_id: uuid.UUID, done: bool) -> None:
    recipe = grocery_store.get_recipe(session, recipe_id)
    if recipe is None:
        raise LookupError("Recipe not found")
    recipe.done_at = datetime.now(timezone.utc) if done else None
    session.commit()


def rate_recipe(session: Session, recipe_id: uuid.UUID, rating: int) -> None:
    recipe = grocery_store.get_recipe(session, recipe_id)
    if recipe is None:
        raise LookupError("Recipe not found")
    recipe.rating = rating or None
    session.commit()


async def update_settings(session: Session, edit: GrocerySettingsUpdate) -> GrocerySettings:
    """Saves the settings; a change to what gets cooked replans this week, a
    new store or shop-by time moves the open shopping task."""
    row = grocery_store.settings(session)
    before = (row.diet, row.recipes_per_week, row.servings)
    store = (row.store_location, row.shop_by_time)
    if edit.diet is not None:
        row.diet = edit.diet
    if edit.recipes_per_week is not None:
        row.recipes_per_week = edit.recipes_per_week
    if edit.servings is not None:
        row.servings = edit.servings
    if edit.store_location is not None:
        row.store_location = edit.store_location.strip() or None
    if edit.shop_by_time is not None:
        row.shop_by_time = edit.shop_by_time.replace(second=0, microsecond=0)
    session.commit()
    if (row.diet, row.recipes_per_week, row.servings) != before:
        request_plan()
    if (row.store_location, row.shop_by_time) != store:
        await shopping.sync_store(session)
    return row


def _today() -> date:
    return to_user_tz(datetime.now(timezone.utc)).date()
