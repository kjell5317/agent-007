"""The grocery agent's two calls: the weekly plan (recipes and the list
entries they still need) and the quick re-check after storage changes (small
adjustments to the agent's own entries). Both may say when the user has to go
shopping. Applying the answers is `services/grocery/`'s job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.agent.grocery.products import ProductLookupError, parse_facts
from app.agent.helpers.llm import chat, user_message
from app.agent.prompts.grocery import PLANNER_SYSTEM_PROMPT, RECHECK_SYSTEM_PROMPT
from app.config import get_settings
from app.db.schemas.grocery import CATEGORIES, UNITS
from app.services.grocery.usage import WeeklyAmount

_PLAN_TOOL_NAME = "set_plan"
_RECHECK_TOOL_NAME = "adjust_list"
# A week of recipes with ingredients and steps runs to a few thousand tokens,
# and Gemini's thinking spends the same budget (see services/task/split.py).
_PLAN_MAX_TOKENS = 16384
_RECHECK_MAX_TOKENS = 2048
_ORIGIN = {"manual": "added by the user", "staple": "starred, restocked when it runs out"}
_ENTRY = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "quantity": {"type": "number"},
        "unit": {"type": "string", "enum": list(UNITS)},
        "portion": {"type": "number"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "shelf_life_days": {"type": "integer"},
        "recipes": {"type": "array", "items": {"type": "string"}},
        "needed": {"type": "number"},
    },
    "required": [
        "name", "quantity", "unit", "portion", "category", "shelf_life_days", "recipes",
        "needed",
    ],
}
_SHOP_BY = {"type": "string", "description": "YYYY-MM-DD, or empty."}
_PLAN_TOOL = {
    "name": _PLAN_TOOL_NAME,
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
            "shopping": {"type": "array", "items": _ENTRY},
            "shop_by": _SHOP_BY,
        },
        "required": ["recipes", "shopping", "shop_by"],
    },
}
_RECHECK_TOOL = {
    "name": _RECHECK_TOOL_NAME,
    "description": "Changes to your entries on the shopping list after storage changed.",
    "parameters": {
        "type": "object",
        "properties": {
            "add": {"type": "array", "items": _ENTRY},
            "update": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "quantity": {"type": "number"}},
                    "required": ["id", "quantity"],
                },
            },
            "drop": {"type": "array", "items": {"type": "string"}},
            "shop_by": _SHOP_BY,
        },
        "required": ["add", "update", "drop", "shop_by"],
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
    # How many to plan: the week's count minus the ones already cooked.
    recipes: int
    servings: int
    storage: list[Stock]
    listed: list[Listed]
    usual: list[WeeklyAmount]
    expired: list[str]
    liked: list[str]
    disliked: list[str]
    cooked: list[str]


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
    # What those recipes use of it in total, in unit — the amount storage
    # has to cover for the entry to be dropped.
    needed: float = 0.0


@dataclass(frozen=True)
class Plan:
    recipes: list[PlannedRecipe]
    shopping: list[ShoppingNeed]
    shop_by: date | None = None


@dataclass(frozen=True)
class Entry:
    """An open list entry as the re-check sees it; only the agent's own carry
    an id it may name."""

    id: str
    name: str
    quantity: float
    unit: str
    origin: str
    recipes: list[str]


@dataclass(frozen=True)
class RecheckContext:
    today: date
    changes: list[str]
    diet: str
    servings: int
    storage: list[Stock]
    entries: list[Entry]
    recipes: list[PlannedRecipe]
    usual: list[WeeklyAmount]


@dataclass(frozen=True)
class Adjustment:
    add: list[ShoppingNeed]
    update: dict[str, float]
    drop: list[str]
    shop_by: date | None


async def plan(context: PlannerContext) -> Plan:
    raw = await _call(
        planner_message(context),
        system_prompt=PLANNER_SYSTEM_PROMPT,
        tool=_PLAN_TOOL,
        max_tokens=_PLAN_MAX_TOKENS,
        name="grocery-plan",
        target="grocery_plan",
    )
    return parse_plan(raw, recipes=context.recipes, today=context.today)


async def recheck(context: RecheckContext) -> Adjustment:
    raw = await _call(
        recheck_message(context),
        system_prompt=RECHECK_SYSTEM_PROMPT,
        tool=_RECHECK_TOOL,
        max_tokens=_RECHECK_MAX_TOKENS,
        name="grocery-recheck",
        target="grocery",
    )
    return parse_adjustment(
        raw, today=context.today, titles={recipe.title for recipe in context.recipes}
    )


async def _call(
    message: str,
    *,
    system_prompt: str,
    tool: dict[str, Any],
    max_tokens: int,
    name: str,
    target: str,
) -> dict[str, Any]:
    """`target` picks the per-agent model: the plan runs on Gemini, the quick
    re-check on the small grocery model. Without a Gemini key, a google
    target falls back to the global default."""
    settings = get_settings()
    provider, model = settings.llm_target(target)
    gemini = provider in ("google", "gemini")
    if gemini and not settings.gemini_api_key:
        provider, model, gemini = None, None, False
    try:
        response = await chat(
            [user_message(message)],
            settings,
            system_prompt=system_prompt,
            tools=[tool],
            force_tool=tool["name"],
            provider=provider,
            model=model,
            thinking_level=settings.chat_thinking_level if gemini else None,
            max_tokens=max_tokens,
            name=name,
        )
    except Exception as exc:  # noqa: BLE001 — any provider failure fails this call
        raise PlanError("The grocery agent could not be reached") from exc
    call = next((c for c in response.tool_calls if c.name == tool["name"]), None)
    if call is None:
        if response.stop_reason in ("length", "max_tokens"):
            raise PlanError("The grocery agent hit its output limit")
        raise PlanError("The grocery agent returned no answer")
    return call.input


def planner_message(context: PlannerContext) -> str:
    lines = [
        f"Today is {context.today:%A}, {context.today.isoformat()}. "
        f"Plan the week starting Monday {context.week_start.isoformat()}.",
        f"Diet: {context.diet}",
        f"Recipes: {context.recipes}, each for {context.servings} "
        f"serving{'s' if context.servings != 1 else ''}",
        *(
            [f"Already cooked this week: {'; '.join(context.cooked)}"]
            if context.cooked else []
        ),
        "",
        *_storage_lines(context.storage),
        "",
        "On the shopping list already:",
        *(
            f"- {i.name}: {_amount(i.quantity, i.unit)} ({_ORIGIN.get(i.origin, i.origin)})"
            for i in context.listed
        ),
        *([] if context.listed else ["(nothing)"]),
        "",
        *_usual_lines(context.usual),
    ]
    if context.expired:
        lines += ["", f"Often expired before it was used: {', '.join(context.expired)}"]
    if context.liked:
        lines += ["", f"Liked recipes: {'; '.join(context.liked)}"]
    if context.disliked:
        lines += ["", f"Disliked recipes: {'; '.join(context.disliked)}"]
    return "\n".join(lines)


def recheck_message(context: RecheckContext) -> str:
    return "\n".join([
        f"Today is {context.today:%A}, {context.today.isoformat()}.",
        f"Diet: {context.diet}; recipes are for {context.servings} "
        f"serving{'s' if context.servings != 1 else ''}.",
        "",
        "What just changed:",
        *(f"- {change}" for change in context.changes),
        "",
        *_storage_lines(context.storage),
        "",
        "This week's recipes:",
        *(
            f"- {recipe.title}: "
            + ", ".join(f"{i['name']} {i['amount']}".strip() for i in recipe.ingredients)
            for recipe in context.recipes
        ),
        *([] if context.recipes else ["(none planned)"]),
        "",
        "On the shopping list:",
        *(_entry_line(entry) for entry in context.entries),
        *([] if context.entries else ["(nothing)"]),
        "",
        *_usual_lines(context.usual),
    ])


def _entry_line(entry: Entry) -> str:
    amount = _amount(entry.quantity, entry.unit)
    if entry.origin != "agent":
        return f"- {entry.name}: {amount} ({_ORIGIN.get(entry.origin, entry.origin)})"
    purpose = f", for {', '.join(entry.recipes)}" if entry.recipes else ""
    return f"- id={entry.id}: {entry.name}: {amount} (yours{purpose})"


def _storage_lines(storage: list[Stock]) -> list[str]:
    return [
        "In storage (amount left, good until):",
        *(
            f"- {s.name}: {_amount(s.quantity, s.unit)}, until {s.expires_on.isoformat()}"
            for s in storage
        ),
        *([] if storage else ["(nothing)"]),
    ]


def _usual_lines(usual: list[WeeklyAmount]) -> list[str]:
    return [
        "Usually bought per week:",
        *(f"- {u.name}: {_amount(u.quantity, u.unit)}" for u in usual),
        *([] if usual else ["(no history yet)"]),
    ]


def _amount(quantity: float, unit: str) -> str:
    return f"{round(quantity, 2):g} {unit}"


def parse_plan(raw: dict[str, Any], *, recipes: int, today: date) -> Plan:
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
    planned = planned[:recipes]
    return Plan(
        recipes=planned,
        shopping=parse_needs(raw.get("shopping"), titles={r.title for r in planned}),
        shop_by=parse_shop_by(raw.get("shop_by"), today=today),
    )


def parse_adjustment(raw: dict[str, Any], *, today: date, titles: set[str]) -> Adjustment:
    update: dict[str, float] = {}
    for item in _list(raw.get("update")):
        try:
            quantity = float(item.get("quantity"))
        except (TypeError, ValueError):
            continue
        if quantity > 0 and str(item.get("id") or "").strip():
            update[str(item["id"]).strip()] = quantity
    drop = raw.get("drop") if isinstance(raw.get("drop"), list) else []
    return Adjustment(
        add=parse_needs(raw.get("add"), titles=titles),
        update=update,
        drop=[str(item_id).strip() for item_id in drop if str(item_id).strip()],
        shop_by=parse_shop_by(raw.get("shop_by"), today=today),
    )


def parse_needs(value: Any, *, titles: set[str]) -> list[ShoppingNeed]:
    needs: list[ShoppingNeed] = []
    for item in _list(value):
        try:
            facts = parse_facts({**item, "pack_quantity": item.get("quantity")})
        except ProductLookupError:
            continue
        needs.append(ShoppingNeed(
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
            needed=_amount_or_zero(item.get("needed")),
        ))
    return needs


def _amount_or_zero(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def parse_shop_by(value: Any, *, today: date) -> date | None:
    """A day in the past means as soon as possible."""
    try:
        day = date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None
    return max(day, today)


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
