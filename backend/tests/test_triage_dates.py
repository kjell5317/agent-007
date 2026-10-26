from __future__ import annotations

import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers.jev import Answer  # noqa: E402
from app.agent.triage.dates import assemble_due, due_from_urgency, resolve_weekday  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
# Wednesday afternoon.
NOW = datetime(2026, 10, 21, 15, 10, tzinfo=TZ)


def _a(value: str, p: float = 0.95) -> Answer:
    return Answer(value=value, p=p)


def _parts(**values) -> dict[str, Answer]:
    defaults = {
        "due_mode": "none",
        "due_month": "none",
        "due_day": "none",
        "due_year": "none",
        "due_anchor": "none",
        "due_weekday": "none",
        "due_week": "none",
        "due_hour": "none",
        "due_minute": "00",
    }
    defaults.update(values)
    return {name: _a(value) for name, value in defaults.items()}


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("today", datetime(2026, 10, 21, 23, 45, tzinfo=TZ)),
        ("tomorrow", datetime(2026, 10, 22, 23, 45, tzinfo=TZ)),
        ("this_week", datetime(2026, 10, 23, 23, 45, tzinfo=TZ)),
        ("next_week", datetime(2026, 10, 30, 23, 45, tzinfo=TZ)),
        ("this_month", datetime(2026, 11, 11, 23, 45, tzinfo=TZ)),
        ("no_rush", datetime(2026, 11, 18, 23, 45, tzinfo=TZ)),
    ],
)
def test_due_from_urgency(bucket, expected):
    assert due_from_urgency(bucket, NOW) == expected


def test_this_week_on_a_weekend_means_sunday():
    saturday = datetime(2026, 10, 24, 10, 0, tzinfo=TZ)
    assert due_from_urgency("this_week", saturday) == datetime(2026, 10, 25, 23, 45, tzinfo=TZ)


def test_today_late_in_the_evening_keeps_an_hour_of_lead():
    late = datetime(2026, 10, 21, 23, 30, tzinfo=TZ)
    assert due_from_urgency("today", late) == datetime(2026, 10, 22, 0, 30, tzinfo=TZ)


def test_resolve_weekday_conventions():
    wednesday = date(2026, 10, 21)
    assert resolve_weekday(wednesday, "Friday", "none") == date(2026, 10, 23)
    assert resolve_weekday(wednesday, "Monday", "none") == date(2026, 10, 26)
    assert resolve_weekday(wednesday, "Friday", "next") == date(2026, 10, 30)
    assert resolve_weekday(wednesday, "Monday", "current") == date(2026, 10, 19)


def test_absolute_date_with_time():
    due, weakest = assemble_due(
        _parts(
            due_mode="absolute",
            due_month="November",
            due_day="3",
            due_year="2026",
            due_hour="14",
            due_minute="30",
        ),
        NOW,
    )
    assert due == datetime(2026, 11, 3, 14, 30, tzinfo=TZ)
    assert weakest == pytest.approx(0.95)


def test_absolute_date_without_year_rolls_to_next_year_when_long_past():
    due, _ = assemble_due(_parts(due_mode="absolute", due_month="January", due_day="10"), NOW)
    assert due == datetime(2027, 1, 10, 23, 45, tzinfo=TZ)


def test_absolute_date_without_year_stays_this_year_when_recent():
    due, _ = assemble_due(_parts(due_mode="absolute", due_month="October", due_day="1"), NOW)
    assert due.date() == date(2026, 10, 1)


def test_impossible_or_incomplete_dates_are_none():
    assert (
        assemble_due(
            _parts(due_mode="absolute", due_month="February", due_day="30", due_year="2027"), NOW
        )[0]
        is None
    )
    assert assemble_due(_parts(due_mode="absolute", due_month="March"), NOW)[0] is None
    assert (
        assemble_due(
            _parts(due_mode="absolute", due_month="March", due_day="2", due_year="other"), NOW
        )[0]
        is None
    )
    assert assemble_due(_parts(due_mode="none"), NOW)[0] is None


def test_relative_dates():
    assert assemble_due(_parts(due_mode="relative", due_anchor="tomorrow"), NOW)[0] == datetime(
        2026, 10, 22, 23, 45, tzinfo=TZ
    )
    due, _ = assemble_due(
        _parts(due_mode="relative", due_anchor="weekday", due_weekday="Friday", due_hour="09"),
        NOW,
    )
    assert due == datetime(2026, 10, 23, 9, 0, tzinfo=TZ)


def test_weakest_part_is_reported():
    parts = _parts(due_mode="relative", due_anchor="weekday", due_weekday="Friday")
    parts["due_weekday"] = _a("Friday", 0.4)
    _, weakest = assemble_due(parts, NOW)
    assert weakest == pytest.approx(0.4)
    assert assemble_due(parts, NOW)[0].time() == time(23, 45)
