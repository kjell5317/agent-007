from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pydantic
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.db.schemas.task import TaskRecurrence  # noqa: E402
from app.services import recurrence  # noqa: E402
from app.services.plan import schedule as schedule_service  # noqa: E402
from app.services.task import recur  # noqa: E402
from app.timezones import to_user_tz, user_tz  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


def test_month_shift_clamps_without_drifting():
    anchor = datetime(2027, 1, 31, 23, 45, tzinfo=BERLIN)

    assert recurrence.shift(anchor, "month", 1).date().isoformat() == "2027-02-28"
    assert recurrence.shift(anchor, "month", 2).date().isoformat() == "2027-03-31"
    assert recurrence.shift(anchor, "month", -1).date().isoformat() == "2026-12-31"


def test_day_shift_keeps_wall_clock_time_across_dst():
    before_switch = datetime(2026, 3, 28, 23, 45, tzinfo=BERLIN)

    after = recurrence.shift(before_switch, "day", 1)

    assert (after.hour, after.minute) == (23, 45)
    assert after.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(
    ("unit", "interval", "ok"),
    [("day", 6, True), ("day", 7, False), ("week", 4, True), ("week", 5, False),
     ("month", 12, True), ("month", 13, False), ("day", 0, False), ("year", 1, False)],
)
def test_interval_limits_fit_the_next_unit(unit, interval, ok):
    if ok:
        recurrence.validate(unit, interval)
    else:
        with pytest.raises(ValueError):
            recurrence.validate(unit, interval)


def test_recurrence_body_rejects_unknown_unit_and_zero_interval():
    with pytest.raises(pydantic.ValidationError):
        TaskRecurrence(unit="year", interval=1)
    with pytest.raises(pydantic.ValidationError):
        TaskRecurrence(unit="day", interval=0)


def test_daily_series_fills_one_week_ahead():
    now = datetime(2026, 10, 18, 9, 0, tzinfo=BERLIN)
    anchor = now.replace(hour=23, minute=45)

    dues = recurrence.occurrences_to_create(
        anchor, "day", 1, latest_due=anchor, now=now, has_upcoming=True
    )

    assert [d.day for d in dues] == [19, 20, 21, 22, 23, 24]
    assert all(d <= now + recurrence.LOOKAHEAD for d in dues)


def test_weekly_series_waits_while_an_instance_is_upcoming():
    now = datetime(2026, 10, 18, 9, 0, tzinfo=BERLIN)
    anchor = now + timedelta(days=2)

    assert recurrence.occurrences_to_create(
        anchor, "week", 1, latest_due=anchor, now=now, has_upcoming=True
    ) == []


def test_monthly_series_plans_next_instance_beyond_the_week_once_nothing_is_upcoming():
    now = datetime(2026, 10, 18, 9, 0, tzinfo=BERLIN)
    anchor = now + timedelta(days=2)

    dues = recurrence.occurrences_to_create(
        anchor, "month", 1, latest_due=anchor, now=now, has_upcoming=False
    )

    assert dues == [datetime(2026, 11, 20, 9, 0, tzinfo=BERLIN)]


def test_missed_occurrences_are_skipped():
    now = datetime(2026, 10, 18, 9, 0, tzinfo=BERLIN)
    anchor = now - timedelta(weeks=3, hours=1)

    dues = recurrence.occurrences_to_create(
        anchor, "week", 2, latest_due=anchor, now=now, has_upcoming=False
    )

    assert dues == [anchor + timedelta(weeks=4)]


def test_create_due_instances_copies_the_latest_instance(monkeypatch):
    now = datetime.now(user_tz())
    series_id = uuid.uuid4()
    anchor = now - timedelta(days=2)
    template = SimpleNamespace(
        id=uuid.uuid4(), title="Pay rent", description="Transfer", link=None,
        estimation=10, location=None, label="admin", due_date=anchor,
        recurrence_unit="month", recurrence_interval=1,
        recurrence_anchor=anchor, recurrence_series_id=series_id,
    )
    payloads = []

    def fake_create(session, payload):
        payloads.append(payload)
        return SimpleNamespace(id=uuid.uuid4(), title=payload.title)

    added = []
    session = SimpleNamespace(add=added.append, commit=lambda: None)
    monkeypatch.setattr(recur.tasks_store, "series", lambda s, sid: [template])
    monkeypatch.setattr(recur.tasks_store, "latest_status_for", lambda s, ids: {template.id: "closed"})
    monkeypatch.setattr(recur.tasks_store, "create", fake_create)

    created = recur.create_due_instances(session, now, [series_id])

    assert len(created) == 1
    payload = payloads[0]
    assert payload.due_date == recurrence.shift(to_user_tz(anchor), "month", 1)
    assert (payload.title, payload.estimation, payload.label) == ("Pay rent", 10, "admin")
    assert (payload.recurrence_unit, payload.recurrence_interval) == ("month", 1)
    assert payload.recurrence_series_id == series_id
    assert added[0].source == "recurrence"
    assert added[0].status == "open"
    assert added[0].task_id == created[0].id


@pytest.mark.parametrize(("status", "created"), [("open", 0), ("closed", 1)])
def test_a_late_open_occurrence_holds_the_series(monkeypatch, status, created):
    now = datetime.now(user_tz())
    series_id = uuid.uuid4()
    due = now - timedelta(days=1)
    late = SimpleNamespace(
        id=uuid.uuid4(), title="Weekly review", description=None, link=None,
        estimation=180, location=None, label=None, due_date=due, is_container=True,
        recurrence_unit="week", recurrence_interval=1,
        recurrence_anchor=due, recurrence_series_id=series_id,
    )
    monkeypatch.setattr(recur.tasks_store, "series", lambda s, sid: [late])
    monkeypatch.setattr(recur.tasks_store, "latest_status_for", lambda s, ids: {late.id: status})
    monkeypatch.setattr(
        recur.tasks_store, "create",
        lambda session, payload: SimpleNamespace(id=uuid.uuid4(), title=payload.title),
    )
    session = SimpleNamespace(add=lambda row: None, commit=lambda: None)

    assert len(recur.create_due_instances(session, now, [series_id])) == created


def test_subtasks_cannot_repeat(monkeypatch):
    subtask = SimpleNamespace(parent_task_id=uuid.uuid4(), is_container=False)
    monkeypatch.setattr(recur.tasks_store, "get", lambda session, task_id: subtask)

    with pytest.raises(ValueError, match="can't repeat"):
        recur.set_recurrence(SimpleNamespace(), uuid.uuid4(), "day", 1)


@pytest.mark.asyncio
async def test_planner_keeps_a_repeat_instance_inside_its_period(monkeypatch):
    due = datetime.now(timezone.utc) + timedelta(days=3)
    task = SimpleNamespace(
        id=uuid.uuid4(), title="Stretch", due_date=due, scheduled_date=None,
        calendar_event_id=None, estimation=30,
        recurrence_unit="day", recurrence_interval=1,
    )
    windows = []
    slot_start = to_user_tz(due).replace(hour=12, minute=0, second=0, microsecond=0)

    def fake_find_free_slot(busy, duration, window_start, window_end, gaps, *, extended_window=False):
        windows.append((window_start, window_end))
        return slot_start, slot_start + timedelta(minutes=30)

    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(
            commute_event_buffer_minutes=0, event_buffer_minutes=0,
            google_calendar_default_event_minutes=30, google_calendar_id="",
            google_busy_calendar_ids=[], slot_min_lead_minutes=0,
            commute_enabled=False, google_maps_api_key="",
        ),
    )
    monkeypatch.setattr(schedule_service, "_find_free_slot", fake_find_free_slot)
    monkeypatch.setattr(
        schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy
    )

    await schedule_service.plan_task_slot(SimpleNamespace(), task)

    window_start, window_end = windows[0]
    assert window_start == to_user_tz(due) - timedelta(days=1)
    assert window_end == to_user_tz(due)


def test_a_split_occurrence_can_take_a_repeat_rule(monkeypatch):
    due = datetime(2026, 10, 20, 23, 45, tzinfo=timezone.utc)
    container = SimpleNamespace(
        id=uuid.uuid4(), parent_task_id=None, is_container=True, due_date=due,
        recurrence_unit=None, recurrence_interval=None, recurrence_series_id=None,
    )
    applied = {}
    monkeypatch.setattr(recur.tasks_store, "get", lambda session, task_id: container)
    monkeypatch.setattr(recur.tasks_store, "series", lambda session, series_id: [])
    monkeypatch.setattr(
        recur.tasks_store, "set_series_recurrence",
        lambda session, series_id, **rule: applied.update(series_id=series_id, **rule),
    )
    session = SimpleNamespace(flush=lambda: None, commit=lambda: None)

    task, stale = recur.set_recurrence(session, container.id, "week", 1)

    assert stale == []
    assert applied == {
        "series_id": container.recurrence_series_id, "unit": "week", "interval": 1, "anchor": due,
    }


def test_rule_change_keeps_split_and_completed_occurrences(monkeypatch):
    series_id = uuid.uuid4()
    due = datetime(2026, 10, 20, 23, 45, tzinfo=timezone.utc)

    def occurrence(days, *, split=False):
        return SimpleNamespace(
            id=uuid.uuid4(), parent_task_id=None, is_container=split,
            due_date=due + timedelta(days=days), recurrence_unit="day",
            recurrence_interval=1, recurrence_series_id=series_id,
        )

    current, split, done, plain = occurrence(0), occurrence(1, split=True), occurrence(2), occurrence(3)
    monkeypatch.setattr(recur.tasks_store, "get", lambda session, task_id: current)
    monkeypatch.setattr(recur.tasks_store, "series", lambda session, sid: [current, split, done, plain])
    monkeypatch.setattr(
        recur.tasks_store, "latest_status_for",
        lambda session, ids: {done.id: "closed"},
    )
    monkeypatch.setattr(recur.tasks_store, "set_series_recurrence", lambda *a, **k: None)
    monkeypatch.setattr(recur, "publish_task", lambda session, task_id: None)
    session = SimpleNamespace(flush=lambda: None, commit=lambda: None)

    _, stale = recur.set_recurrence(session, current.id, "week", 1)

    assert stale == [plain.id]


@pytest.mark.asyncio
async def test_planner_keeps_subtasks_of_a_split_occurrence_inside_its_period(monkeypatch):
    parent_due = datetime.now(timezone.utc) + timedelta(days=3)
    parent = SimpleNamespace(
        id=uuid.uuid4(), due_date=parent_due, recurrence_unit="day", recurrence_interval=1,
    )
    subtask = SimpleNamespace(
        id=uuid.uuid4(), title="Stretch I", due_date=parent_due - timedelta(minutes=45),
        scheduled_date=None, calendar_event_id=None, estimation=30,
        parent_task_id=parent.id, recurrence_unit=None,
    )
    windows = []
    slot_start = to_user_tz(subtask.due_date).replace(hour=12, minute=0, second=0, microsecond=0)

    def fake_find_free_slot(busy, duration, window_start, window_end, gaps, *, extended_window=False):
        windows.append(window_start)
        return slot_start, slot_start + timedelta(minutes=30)

    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(
            commute_event_buffer_minutes=0, event_buffer_minutes=0,
            google_calendar_default_event_minutes=30, google_calendar_id="",
            google_busy_calendar_ids=[], slot_min_lead_minutes=0,
            commute_enabled=False, google_maps_api_key="",
        ),
    )
    monkeypatch.setattr(schedule_service.tasks_store, "get", lambda session, task_id: parent)
    monkeypatch.setattr(schedule_service, "_find_free_slot", fake_find_free_slot)
    monkeypatch.setattr(
        schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy
    )

    await schedule_service.plan_task_slot(SimpleNamespace(), subtask)

    assert windows[0] == to_user_tz(parent_due) - timedelta(days=1)
