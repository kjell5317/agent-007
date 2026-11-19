"""Product facts from the cache, or from one LLM lookup that fills it."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.agent.grocery.products import lookup
from app.db.clients import grocery as grocery_store
from app.db.models.grocery import GroceryProduct
from app.services.grocery.normalize import product_key


async def resolve(session: Session, name: str) -> GroceryProduct:
    key = product_key(name)
    if not key:
        raise ValueError("Enter a product name")
    cached = grocery_store.find_product(session, key)
    if cached is not None:
        return cached
    facts = await lookup(name)
    # Different spellings of one product ("tomato", "Tomatoes") land on the
    # canonical name's row, so they share its facts and its star.
    canonical = product_key(facts.name) or key
    existing = grocery_store.get_product(session, canonical)
    if existing is not None:
        grocery_store.add_alias(existing, key)
        return existing
    return grocery_store.add_product(
        session,
        key=canonical,
        alias=key,
        name=facts.name,
        category=facts.category,
        unit=facts.unit,
        pack_quantity=facts.pack_quantity,
        portion=facts.portion,
        shelf_life_days=facts.shelf_life_days,
    )
