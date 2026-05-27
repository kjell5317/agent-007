from datetime import datetime, timedelta
from importlib import import_module
from zoneinfo import ZoneInfo

from app.services.plan.schedule import BusyEvent

schedule_module = import_module("app.services.plan.schedule")


def test_free_slot_search_starts_first_day_at_20(monkeypatch):
    tz = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr(schedule_module, "_user_tz", lambda: tz)

    start = datetime(2026, 1, 1, 9, 0, tzinfo=tz)
    end = datetime(2026, 1, 3, 21, 0, tzinfo=tz)

    slot = schedule_module._find_free_slot([], timedelta(minutes=30), start, end, timedelta(minutes=5))

    assert slot == (
        datetime(2026, 1, 1, 19, 30, tzinfo=tz),
        datetime(2026, 1, 1, 20, 0, tzinfo=tz),
    )


def test_free_slot_search_moves_backwards_inside_day(monkeypatch):
    tz = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr(schedule_module, "_user_tz", lambda: tz)

    start = datetime(2026, 1, 1, 9, 0, tzinfo=tz)
    end = datetime(2026, 1, 2, 21, 0, tzinfo=tz)
    busy = [
        BusyEvent(
            id="event",
            start=datetime(2026, 1, 1, 19, 0, tzinfo=tz),
            end=datetime(2026, 1, 1, 20, 0, tzinfo=tz),
            kind="busy",
        )
    ]

    slot = schedule_module._find_free_slot(
        busy,
        timedelta(minutes=30),
        start,
        end,
        timedelta(minutes=5),
    )

    assert slot == (
        datetime(2026, 1, 1, 18, 25, tzinfo=tz),
        datetime(2026, 1, 1, 18, 55, tzinfo=tz),
    )


def test_free_slot_search_advances_day_after_exhausting_10_to_20(monkeypatch):
    tz = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr(schedule_module, "_user_tz", lambda: tz)

    start = datetime(2026, 1, 1, 9, 0, tzinfo=tz)
    end = datetime(2026, 1, 2, 21, 0, tzinfo=tz)
    busy = [
        BusyEvent(
            id="event",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=tz),
            end=datetime(2026, 1, 1, 20, 0, tzinfo=tz),
            kind="busy",
        )
    ]

    slot = schedule_module._find_free_slot(
        busy,
        timedelta(minutes=30),
        start,
        end,
        timedelta(minutes=5),
    )

    assert slot == (
        datetime(2026, 1, 2, 19, 30, tzinfo=tz),
        datetime(2026, 1, 2, 20, 0, tzinfo=tz),
    )
