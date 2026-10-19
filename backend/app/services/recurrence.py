"""Repeat rules for recurring tasks — pure date math, no I/O.

A series repeats every `interval` days, weeks or months counted from its
anchor (the due date the rule was set on). Callers pass datetimes in the
user's zone so occurrences keep the anchor's wall-clock time across DST, and
months clamp to the month's last day without drifting (31st → 28th → 31st).
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

# Each unit stops where the next one takes over: a week has 7 days, a month
# about 4 weeks, a year 12 months.
MAX_INTERVAL: dict[str, int] = {"day": 6, "week": 4, "month": 12}

# Like commute legs, instances are only materialized a week ahead.
LOOKAHEAD = timedelta(days=7)


def validate(unit: str, interval: int) -> None:
    limit = MAX_INTERVAL.get(unit)
    if limit is None:
        raise ValueError(f"Unknown repeat unit: {unit}")
    if not 1 <= interval <= limit:
        raise ValueError(f"A {unit} interval must be between 1 and {limit}")


def shift(anchor: datetime, unit: str, count: int) -> datetime:
    if unit == "day":
        return anchor + timedelta(days=count)
    if unit == "week":
        return anchor + timedelta(weeks=count)
    months = anchor.month - 1 + count
    year, month = anchor.year + months // 12, months % 12 + 1
    return anchor.replace(
        year=year, month=month, day=min(anchor.day, calendar.monthrange(year, month)[1])
    )


def occurrences_to_create(
    anchor: datetime,
    unit: str,
    interval: int,
    *,
    latest_due: datetime,
    now: datetime,
    has_upcoming: bool,
) -> list[datetime]:
    """Due dates a series still lacks: every occurrence after its latest
    instance up to a week out, and — when nothing open lies ahead — the next
    one however far away it is. Occurrences already in the past are skipped."""
    step = 1
    while (due := shift(anchor, unit, step * interval)) <= max(latest_due, now):
        step += 1
    missing: list[datetime] = []
    while due <= now + LOOKAHEAD:
        missing.append(due)
        step += 1
        due = shift(anchor, unit, step * interval)
    if not missing and not has_upcoming:
        missing.append(due)
    return missing
