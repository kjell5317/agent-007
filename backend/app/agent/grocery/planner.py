"""The weekly recipe plan: one LLM call that returns the recipes and the
shopping-list entries they still need. Applying it is
`services/grocery/plan.py`'s job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.agent.grocery.products import ProductLookupError, parse_facts
from app.agent.helpers.llm import chat, user_message
from app.agent.prompts.grocery import PLANNER_SYSTEM_PROMPT
from app.config import get_settings
from app.db.schemas.grocery import CATEGORIES, UNITS
from app.services.grocery.usage import WeeklyAmount

_TOOL_NAME = "set_plan"
# A week of recipes with ingredients and steps runs to a few thousand tokens.
_MAX_TOKENS = 8192
_ORIGIN = {"manual": "added by the user", "staple": "starred, restocked when it runs out"}
_TOOL = {
    "name": _TOOL_NAME,
    "description": "This week's recipes and what to add to the shopping list for them.",
    "parameters": {
        "type": "object",
        "properties": {
            "recipes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "ingredients": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "amount": {"type": "string"},
                                },
                                "required": ["name", "amount"],
                            },
                        },
                        "steps": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "ingredients", "steps"],
                },
            },
            "shopping": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string", "enum": list(UNITS)},
                        "portion": {"type": "number"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "shelf_life_days": {"type": "integer"},
                        "recipes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "name", "quantity", "unit", "portion", "category", "shelf_life_days",
                        "recipes",
                    ],
                },
            },
        },
        "required": ["recipes", "shopping"],
    },
}


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Stock:
    name: str
    quantity: float
    unit: str
    expires_on: date


@dataclass(frozen=True)
class Listed:
    name: str
    quantity: float
    unit: str
    origin: str


@dataclass(frozen=True)
class PlannerContext:
    today: date
    week_start: date
    diet: str
    recipes: int
    servings: int
    storage: list[Stock]
    listed: list[Listed]
    usual: list[WeeklyAmount]
    expired: list[str]
    liked: list[str]
    disliked: list[str]


@dataclass(frozen=True)
class PlannedRecipe:
    title: str
    ingredients: list[dict[str, str]]
    steps: list[str]


@dataclass(frozen=True)
class ShoppingNeed:
    name: str
    quantity: float
    unit: str
    portion: float
    category: str
    shelf_life_days: int
    # Titles of the planned recipes it's for; empty for a usual purchase.
    recipes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    recipes: list[PlannedRecipe]
    shopping: list[ShoppingNeed]


async def plan(context: PlannerContext) -> Plan:
    settings = get_settings()
    try:
        response = await chat(
            [user_message(planner_message(context))],
            settings,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            tools=[_TOOL],
            force_tool=_TOOL_NAME,
            max_tokens=_MAX_TOKENS,
            name="grocery-plan",
        )
    except Exception as exc:  # noqa: BLE001 — any provider failure fails this plan
        raise PlanError("The planner could not be reached") from exc
    call = next((c for c in response.tool_calls if c.name == _TOOL_NAME), None)
    if call is None:
        if response.stop_reason in ("length", "max_tokens"):
            raise PlanError("The planner hit its output limit")
        raise PlanError("The planner returned no plan")
    return parse_plan(call.input, recipes=context.recipes)


def planner_message(context: PlannerContext) -> str:
    lines = [
        f"Today is {context.today:%A}, {context.today.isoformat()}. "
        f"Plan the week starting Monday {context.week_start.isoformat()}.",
        f"Diet: {context.diet}",
        f"Recipes: {context.recipes}, each for {context.servings} "
        f"serving{'s' if context.servings != 1 else ''}",
        "",
        "In storage (amount left, good until):",
        *(
            f"- {s.name}: {_amount(s.quantity, s.unit)}, until {s.expires_on.isoformat()}"
            for s in context.storage
        ),
        *([] if context.storage else ["(nothing)"]),
        "",
        "On the shopping list already:",
        *(
            f"- {i.name}: {_amount(i.quantity, i.unit)} ({_ORIGIN.get(i.origin, i.origin)})"
            for i in context.listed
        ),
        *([] if context.listed else ["(nothing)"]),
        "",
        "Usually bought per week:",
        *(f"- {u.name}: {_amount(u.quantity, u.unit)}" for u in context.usual),
        *([] if context.usual else ["(no history yet)"]),
    ]
    if context.expired:
        lines += ["", f"Often expired before it was used: {', '.join(context.expired)}"]
    if context.liked:
        lines += ["", f"Liked recipes: {'; '.join(context.liked)}"]
    if context.disliked:
        lines += ["", f"Disliked recipes: {'; '.join(context.disliked)}"]
    return "\n".join(lines)


def _amount(quantity: float, unit: str) -> str:
    return f"{round(quantity, 2):g} {unit}"


def parse_plan(raw: dict[str, Any], *, recipes: int) -> Plan:
    planned: list[PlannedRecipe] = []
    for item in _list(raw.get("recipes")):
        title = str(item.get("title") or "").strip()[:200]
        ingredients = [
            {
                "name": str(i.get("name")).strip()[:120],
                "amount": str(i.get("amount") or "").strip()[:40],
            }
            for i in _list(item.get("ingredients"))
            if str(i.get("name") or "").strip()
        ]
        steps = [
            str(step).strip() for step in item.get("steps") or [] if str(step).strip()
        ]
        if title and ingredients and steps:
            planned.append(PlannedRecipe(title, ingredients, steps))
    if recipes and not planned:
        raise PlanError("The planner returned no usable recipes")

    titles = {recipe.title for recipe in planned[:recipes]}
    shopping: list[ShoppingNeed] = []
    for item in _list(raw.get("shopping")):
        try:
            facts = parse_facts({**item, "pack_quantity": item.get("quantity")})
        except ProductLookupError:
            continue
        shopping.append(ShoppingNeed(
            name=facts.name,
            quantity=facts.pack_quantity,
            unit=facts.unit,
            portion=facts.portion,
            category=facts.category,
            shelf_life_days=facts.shelf_life_days,
            recipes=tuple(dict.fromkeys(
                str(title).strip() for title in item.get("recipes") or []
                if str(title).strip() in titles
            )),
        ))
    return Plan(recipes=planned[:recipes], shopping=shopping)


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
