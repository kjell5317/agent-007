"""Due dates from Jev answers. Jev reads the parts of a stated deadline; the
calendar arithmetic (weekdays, year roll-over, urgency buckets) lives here."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta

from app.agent.helpers.jev import Answer
from app.agent.triage.questions import MONTHS, WEEKDAYS

END_OF_DAY = time(23, 45)
FRIDAY, SUNDAY = 4, 6


def end_of_day(day: date, now: datetime) -> datetime:
    return datetime.combine(day, END_OF_DAY, tzinfo=now.tzinfo)


def due_from_urgency(bucket: str, now: datetime) -> datetime:
    """Implicit deadline for a task with no stated one; `now` is local time."""
    today = now.date()
    if bucket == "today":
        return max(end_of_day(today, now), now + timedelta(hours=1))
    if bucket == "tomorrow":
        return end_of_day(today + timedelta(days=1), now)
    if bucket == "this_week":
        weekday = today.weekday()
        target = SUNDAY if weekday >= FRIDAY else FRIDAY
        return end_of_day(today + timedelta(days=target - weekday), now)
    if bucket == "next_week":
        next_monday = today + timedelta(days=7 - today.weekday())
        return end_of_day(next_monday + timedelta(days=FRIDAY), now)
    if bucket == "this_month":
        return end_of_day(today + timedelta(days=21), now)
    return end_of_day(today + timedelta(days=28), now)


def resolve_weekday(today: date, weekday: str, week: str) -> date:
    """A bare weekday is the next one on or after today; 'current' is this
    calendar week's, 'next' the following week's."""
    target = WEEKDAYS.index(weekday)
    this_monday = today - timedelta(days=today.weekday())
    if week == "next":
        return this_monday + timedelta(days=7 + target)
    if week == "current":
        return this_monday + timedelta(days=target)
    return today + timedelta(days=(target - today.weekday()) % 7)


def assemble_due(parts: Mapping[str, Answer], now: datetime) -> tuple[datetime | None, float]:
    """`(due, weakest p among the parts used)`; due is None when the parts
    don't add up to a date (or no deadline is stated)."""
    mode = parts["due_mode"]
    used = [mode.p]

    def done(day: date | None) -> tuple[datetime | None, float]:
        if day is None:
            return None, min(used)
        return datetime.combine(day, _time_of_day(parts, used), tzinfo=now.tzinfo), min(used)

    today = now.date()
    if mode.value == "relative":
        anchor = parts["due_anchor"]
        used.append(anchor.p)
        offsets = {"today": 0, "tomorrow": 1, "day_after": 2}
        if anchor.value in offsets:
            return done(today + timedelta(days=offsets[anchor.value]))
        if anchor.value == "weekday":
            weekday, week = parts["due_weekday"], parts["due_week"]
            used += [weekday.p, week.p]
            if weekday.value in WEEKDAYS:
                return done(resolve_weekday(today, weekday.value, week.value))
        return done(None)

    if mode.value == "absolute":
        month, day, year = parts["due_month"], parts["due_day"], parts["due_year"]
        used += [month.p, day.p, year.p]
        if month.value not in MONTHS or not day.value.isdigit() or year.value == "other":
            return done(None)
        stated_year = int(year.value) if year.value.isdigit() else today.year
        try:
            resolved = date(stated_year, MONTHS.index(month.value) + 1, int(day.value))
        except ValueError:
            return done(None)
        # No year given and the date has passed by more than a month: it means next year.
        if not year.value.isdigit() and resolved < today - timedelta(days=31):
            try:
                resolved = resolved.replace(year=today.year + 1)
            except ValueError:
                return done(None)
        return done(resolved)

    return done(None)


def _time_of_day(parts: Mapping[str, Answer], used: list[float]) -> time:
    hour = parts.get("due_hour")
    if hour is None or not hour.value.isdigit():
        return END_OF_DAY
    minute = parts.get("due_minute")
    used.append(hour.p)
    minutes = int(minute.value) if minute is not None and minute.value.isdigit() else 0
    return time(int(hour.value), minutes)
