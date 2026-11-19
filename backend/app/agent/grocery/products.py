"""One LLM call telling how a grocery product is bought, used and kept. Only
reached on a product-cache miss (see `services/grocery/products.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.helpers.llm import chat, user_message
from app.agent.prompts.grocery import PRODUCT_FACTS_SYSTEM_PROMPT
from app.config import get_settings
from app.db.schemas.grocery import CATEGORIES, UNITS

_TOOL_NAME = "product_facts"
_TOOL = {
    "name": _TOOL_NAME,
    "description": "How this grocery product is usually bought, used and kept.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "unit": {"type": "string", "enum": list(UNITS)},
            "pack_quantity": {"type": "number"},
            "portion": {"type": "number"},
            "shelf_life_days": {"type": "integer"},
        },
        "required": ["name", "category", "unit", "pack_quantity", "portion", "shelf_life_days"],
    },
}
_MAX_SHELF_LIFE_DAYS = 3650


class ProductLookupError(Exception):
    pass


@dataclass(frozen=True)
class ProductFacts:
    name: str
    category: str
    unit: str
    pack_quantity: float
    portion: float
    shelf_life_days: int


async def lookup(text: str) -> ProductFacts:
    settings = get_settings()
    provider, model = settings.llm_target("grocery")
    try:
        response = await chat(
            [user_message(text)],
            settings,
            system_prompt=PRODUCT_FACTS_SYSTEM_PROMPT,
            tools=[_TOOL],
            force_tool=_TOOL_NAME,
            provider=provider,
            model=model,
            name="grocery-product-facts",
        )
    except Exception as exc:  # noqa: BLE001 — any provider failure is one lookup failure
        raise ProductLookupError(f"Couldn't look up {text!r}") from exc
    call = next((c for c in response.tool_calls if c.name == _TOOL_NAME), None)
    if call is None:
        raise ProductLookupError(f"Couldn't look up {text!r}")
    return parse_facts(call.input)


def parse_facts(raw: dict[str, Any]) -> ProductFacts:
    name = str(raw.get("name") or "").strip()[:128]
    unit = raw.get("unit")
    category = raw.get("category")
    try:
        pack_quantity = float(raw.get("pack_quantity") or 0)
        portion = float(raw.get("portion") or 0)
        shelf_life_days = int(raw.get("shelf_life_days") or 0)
    except (TypeError, ValueError) as exc:
        raise ProductLookupError("The product lookup returned no usable amounts") from exc
    if not name or unit not in UNITS or pack_quantity <= 0 or portion <= 0 or shelf_life_days <= 0:
        raise ProductLookupError("The product lookup returned no usable facts")
    return ProductFacts(
        name=name,
        category=category if category in CATEGORIES else "other",
        unit=unit,
        pack_quantity=pack_quantity,
        portion=min(portion, pack_quantity),
        shelf_life_days=min(shelf_life_days, _MAX_SHELF_LIFE_DAYS),
    )
