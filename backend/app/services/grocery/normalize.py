"""Pure text and number handling for grocery entries: the cache key a product
name maps to, the amount a manual entry spells out ("2 kg rice", "3 tomatoes",
"½ cabbage") and the date math of storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Dimension = Literal["mass", "volume", "count"]

_NON_WORD = re.compile(r"[^\w\s]|_")
_LEADING_AMOUNT = re.compile(
    r"^(?P<num>\d+/\d+|\d+(?:[.,]\d+)?|[½¼¾])\s*"
    r"(?P<unit>kg|kilos?|grams?|gr|g|liters?|litres?|l|ml|x|×)?(?=\s|$)\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
_FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75}
# Unit → (dimension, factor to the base unit g / ml / piece).
_UNITS: dict[str, tuple[Dimension, float]] = {
    "kg": ("mass", 1000), "kilo": ("mass", 1000), "kilos": ("mass", 1000),
    "g": ("mass", 1), "gr": ("mass", 1), "gram": ("mass", 1), "grams": ("mass", 1),
    "l": ("volume", 1000), "liter": ("volume", 1000), "liters": ("volume", 1000),
    "litre": ("volume", 1000), "litres": ("volume", 1000), "ml": ("volume", 1),
    "x": ("count", 1), "×": ("count", 1),
}
_EPSILON = 1e-9


@dataclass(frozen=True)
class Entry:
    name: str
    amount: float | None = None
    dimension: Dimension | None = None


def product_key(name: str) -> str:
    return " ".join(_NON_WORD.sub(" ", name.casefold()).split())[:128]


def parse_entry(text: str) -> Entry:
    text = " ".join(text.split())
    match = _LEADING_AMOUNT.match(text)
    if match is None or not match["rest"]:
        return Entry(name=text)
    amount = _number(match["num"])
    if amount <= 0:
        return Entry(name=match["rest"])
    unit = (match["unit"] or "x").lower()
    dimension, factor = _UNITS[unit]
    return Entry(name=match["rest"], amount=amount * factor, dimension=dimension)


def _number(raw: str) -> float:
    if raw in _FRACTIONS:
        return _FRACTIONS[raw]
    if "/" in raw:
        top, bottom = raw.split("/")
        return int(top) / int(bottom) if int(bottom) else 0.0
    return float(raw.replace(",", "."))


def entry_quantity(entry: Entry, *, unit: str, pack_quantity: float) -> float:
    """The list amount for an entry in the product's unit. A bare count of a
    weighed product means packs ("2 rice" → two bags); an amount in the wrong
    dimension ("500 g tomatoes" sold by the piece) falls back to the usual pack."""
    if entry.amount is None:
        return pack_quantity
    if entry.dimension == "count":
        return entry.amount * pack_quantity if unit in ("g", "ml") else entry.amount
    if (entry.dimension, unit) in (("mass", "g"), ("volume", "ml")):
        return entry.amount
    return pack_quantity


def expiry_date(bought_on: date, shelf_life_days: int) -> date:
    return bought_on + timedelta(days=shelf_life_days)


def shelf_life_from(added_on: date, expires_on: date) -> int:
    return max(1, (expires_on - added_on).days)


def after_portion(quantity: float, portion: float) -> float | None:
    """What's left after using one portion; None once it's used up."""
    left = quantity - portion
    return left if left > _EPSILON else None
