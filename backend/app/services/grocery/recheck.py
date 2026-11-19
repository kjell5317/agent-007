"""The quick re-check after something leaves storage by hand ("−1 portion" or
remove). Changes collect while the user is busy; five quiet minutes after the
last one, a single small LLM call adjusts the agent's list entries and may
bring the shopping task forward. Crossing off the list, expiry and date edits
don't count.

The timer lives in memory, like the rest of the app's background state: a
restart inside the quiet period drops that one re-check."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agent.grocery.planner import Adjustment, Entry, PlannedRecipe, RecheckContext, Stock
from app.agent.grocery.planner import recheck as run_recheck
from app.db import SessionLocal
from app.db.clients import grocery as grocery_store
from app.events import publish_grocery
from app.services.grocery import plan, shopping
from app.services.grocery.normalize import purchasable, week_start
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

QUIET_S = 300

_changes: list[str] = []
_timer: asyncio.Task | None = None
_lock = asyncio.Lock()


def note(change: str) -> None:
    """Record a storage change and restart the quiet period."""
    global _timer
    _changes.append(change)
    if _timer is not None:
        _timer.cancel()
    _timer = asyncio.create_task(_after_quiet(), name="grocery-recheck")


async def _after_quiet() -> None:
    global _timer
    await asyncio.sleep(QUIET_S)
    # Past the wait, a new change starts its own timer instead of cancelling
    # this run mid-call.
    _timer = None
    async with _lock:
        changes = _changes.copy()
        _changes.clear()
        # A running plan reads the same storage; it covers these changes.
        if not changes or plan.is_planning():
            return
        try:
            with SessionLocal() as session:
                await recheck(session, changes)
        except Exception:  # noqa: BLE001 — the next change or plan catches up
            log.exception("grocery re-check failed")
        publish_grocery()


async def recheck(session: Session, changes: list[str]) -> None:
    now = datetime.now(timezone.utc)
    today = to_user_tz(now).date()
    settings = grocery_store.settings(session)
    context = RecheckContext(
        today=today,
        changes=changes,
        diet=settings.diet,
        servings=settings.servings,
        storage=[
            Stock(s.name, s.quantity, s.unit, s.expires_on)
            for s in grocery_store.active_storage(session)
        ],
        entries=[
            Entry(str(i.id), i.name, i.quantity, i.unit, i.origin, list(i.recipe_titles))
            for i in grocery_store.open_items(session)
        ],
        recipes=[
            PlannedRecipe(r.title, r.ingredients, r.steps)
            for r in grocery_store.recipes_for_week(session, week_start(today))
            if r.done_at is None
        ],
        usual=plan.usual_amounts(session, now),
    )
    # Don't hold a transaction open across the LLM call.
    session.commit()
    adjustment = await run_recheck(context)
    apply_adjustment(session, adjustment)
    session.commit()
    if adjustment.shop_by is not None and grocery_store.open_items(session):
        await shopping.ensure_task(session, adjustment.shop_by)
    log.info(
        "grocery re-check done · changes=%d add=%d update=%d drop=%d shop_by=%s",
        len(changes), len(adjustment.add), len(adjustment.update), len(adjustment.drop),
        adjustment.shop_by,
    )


def apply_adjustment(session: Session, adjustment: Adjustment) -> None:
    # Only the agent's own open entries can change or go; the user's and the
    # starred ones stay exactly as they are, whatever ids come back.
    own = {str(i.id): i for i in grocery_store.open_items(session) if i.origin == "agent"}
    for item_id in adjustment.drop:
        item = own.pop(item_id, None)
        if item is not None:
            grocery_store.delete(session, item)
    for item_id, quantity in adjustment.update.items():
        item = own.get(item_id)
        if item is not None:
            item.quantity = purchasable(
                quantity, unit=item.unit, pack_quantity=item.product.pack_quantity
            )
    plan.add_needs(session, adjustment.add)
