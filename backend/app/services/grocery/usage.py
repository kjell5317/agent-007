"""Pure summaries of the purchase history the planner reads: what the user
usually buys in a week, and what went off before it was used."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Purchase:
    product_key: str
    name: str
    quantity: float
    unit: str


@dataclass(frozen=True)
class WeeklyAmount:
    name: str
    quantity: float
    unit: str


def weeks_observed(first: datetime | None, now: datetime, *, cap: int) -> int:
    """The weeks the history covers, so a new user's first week isn't averaged
    over a month."""
    if first is None:
        return 1
    return max(1, min(cap, math.ceil((now - first).days / 7)))


def weekly_usage(purchases: list[Purchase], *, weeks: int, limit: int = 25) -> list[WeeklyAmount]:
    """Most often bought first. A product bought in two units (the user
    changed it) counts per unit, since the amounts can't be added."""
    totals: dict[tuple[str, str], float] = {}
    names: dict[tuple[str, str], str] = {}
    times: Counter[tuple[str, str]] = Counter()
    for purchase in purchases:
        key = (purchase.product_key, purchase.unit)
        totals[key] = totals.get(key, 0) + purchase.quantity
        names[key] = purchase.name
        times[key] += 1
    ranked = sorted(totals, key=lambda key: (-times[key], names[key]))
    return [WeeklyAmount(names[key], totals[key] / weeks, key[1]) for key in ranked[:limit]]


def often_expired(names: list[str], *, limit: int = 10) -> list[str]:
    return [name for name, _ in Counter(names).most_common(limit)]
