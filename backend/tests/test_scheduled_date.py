from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app import cron  # noqa: E402
from app.api import notifications  # noqa: E402
from app.db.clients import tasks as tasks_store  # noqa: E402
from app.db.clients import points as points_store  # noqa: E402
from app.db.models.points_entry import PointsEntry  # noqa: E402
from app.db.models.task import Task  # noqa: E402
from app.db.schemas.task import TaskRead  # noqa: E402
from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.calendar import discover  # noqa: E402
from app.services.calendar import events as calendar_events  # noqa: E402
from app.services import home_assistant as home_assistant_service  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.services.plan import schedule as schedule_service  # noqa: E402
from app.services.plan.schedule import Interval  # noqa: E402


def _sqlite_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Task.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _sqlite_points_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    PointsEntry.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _event(event_id: str, start: datetime, end: datetime, task_id: uuid.UUID) -> CalendarEvent:
    return CalendarEvent(
        id=event_id,
        calendar_id="primary",
        summary="Task",
        description=None,
        start=start,
        end=end,
        all_day=False,
        location=None,
        html_link=None,
        private_properties={
            "managed_by": "plan_service",
            "kind": "task",
            "task_id": str(task_id),
        },
        raw={},
    )


def test_task_read_serializes_scheduled_date():
    scheduled = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        title="Write report",
        description=None,
        link=None,
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=scheduled,
        calendar_event_id="event-1",
        estimation=30,
        location=None,
        label=None,
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    read = TaskRead.build(row, "open", True)

    assert read.scheduled_date == scheduled
    dumped = read.model_dump(mode="json")
    assert dumped["scheduled_date"] == "2026-07-01T12:30:00Z"
    assert dumped["schedule_status"] == "scheduled"


def test_task_read_marks_open_task_without_calendar_event_unscheduled():
    scheduled = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        title="Write report",
        description=None,
        link=None,
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=scheduled,
        calendar_event_id=None,
        estimation=30,
        location=None,
        label=None,
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    read = TaskRead.build(row, "open", True)

    assert read.scheduled_date == scheduled
    assert read.schedule_status == "unscheduled"


def test_task_read_marks_open_task_without_slot_unscheduled_despite_stale_mirror():
    # #166 repro: the slot was cleared (scheduled_date=None) but a stale
    # calendar_event_id lingers. The task is genuinely unscheduled and must
    # read red, not grey.
    row = SimpleNamespace(
        id=uuid.uuid4(),
        title="Write report",
        description=None,
        link=None,
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=None,
        calendar_event_id="event-stale",
        estimation=30,
        location=None,
        label=None,
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    read = TaskRead.build(row, "open", True)

    assert read.scheduled_date is None
    assert read.schedule_status == "unscheduled"


def test_task_list_orders_by_scheduled_date(monkeypatch):
    session = _sqlite_session()
    created = datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc)
    # Every task carries a scheduled_date now; list order follows that slot,
    # independent of the due date shown on the card.
    early = Task(
        title="Scheduled earliest",
        due_date=datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc),
        scheduled_date=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        calendar_event_id="event-early",
        created_at=created,
    )
    middle = Task(
        title="Scheduled middle",
        due_date=datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc),
        scheduled_date=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        calendar_event_id="event-mid",
        created_at=created + timedelta(minutes=1),
    )
    middle_newer = Task(
        title="Scheduled middle newer",
        due_date=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
        scheduled_date=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        calendar_event_id="event-mid-newer",
        created_at=created + timedelta(minutes=3),
    )
    late = Task(
        title="Scheduled latest",
        scheduled_date=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        calendar_event_id="event-late",
        created_at=created + timedelta(minutes=2),
    )
    session.add_all([late, early, middle, middle_newer])
    session.commit()

    monkeypatch.setattr(
        tasks_store,
        "latest_status_for",
        lambda _session, ids: {task_id: "open" for task_id in ids},
    )

    rows = tasks_store.list_(session)

    assert [task.title for task, _status in rows] == [
        "Scheduled earliest",
        "Scheduled middle newer",
        "Scheduled middle",
        "Scheduled latest",
    ]


def test_open_filter_survives_limit_with_many_closed(monkeypatch):
    # Regression: with schedule-ASC ordering, completed tasks (old scheduled
    # slots) sort first. If `limit` is applied before the status filter, they
    # fill the window and evict every open task. The filter must run in SQL.
    session = _sqlite_session()
    # The ORM RawInput carries a pgvector column that won't create on SQLite,
    # so mirror just the columns the status join reads — with matching UUID
    # types so the join keys align with Task.id.
    raw_inputs = Table(
        "raw_inputs",
        MetaData(),
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("task_id", UUID(as_uuid=True)),
        Column("status", String(32)),
        Column("received_at", DateTime(timezone=True)),
        Column("source", String(64)),
    )
    raw_inputs.create(session.get_bind())
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def add(title: str, *, status: str, due=None, scheduled=None, created):
        # due_date and scheduled_date are both NOT NULL; fall back to created so
        # schedule order is deterministic (closed tasks land oldest, filling the
        # limit window).
        task = Task(
            title=title,
            due_date=due or created,
            scheduled_date=scheduled or created,
            created_at=created,
        )
        session.add(task)
        session.flush()
        session.execute(
            raw_inputs.insert(),
            {"id": uuid.uuid4(), "task_id": task.id, "status": status, "received_at": created, "source": "gmail"},
        )

    for i in range(5):
        add(f"closed-{i}", status="closed", due=base + timedelta(days=i), created=base + timedelta(days=i))
    add("open-scheduled", status="open", scheduled=base + timedelta(days=40), created=base + timedelta(days=40))
    add("open-undated", status="open", created=base + timedelta(days=41))
    session.commit()

    # latest_status_for uses Postgres ANY(:ids); stub it as the ordering test does.
    # The status filtering under test happens in the SQL join, not here.
    monkeypatch.setattr(
        tasks_store, "latest_status_for", lambda _session, ids: {tid: "open" for tid in ids}
    )

    rows = tasks_store.list_(session, status="open", limit=2)

    assert {task.title for task, _status in rows} == {"open-scheduled", "open-undated"}


def test_overdue_open_survives_limit_with_many_closed():
    # Regression: the scheduled_date backfill tied a large pile of *closed*
    # tasks at one instant. If the status filter runs after `limit`, those
    # closed rows fill the window and evict the open tasks that must reschedule.
    session = _sqlite_session()
    raw_inputs = Table(
        "raw_inputs",
        MetaData(),
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("task_id", UUID(as_uuid=True)),
        Column("status", String(32)),
        Column("received_at", DateTime(timezone=True)),
        Column("source", String(64)),
    )
    raw_inputs.create(session.get_bind())
    overdue = datetime(2026, 6, 30, 19, 42, tzinfo=timezone.utc)  # all tied here
    cutoff = datetime(2026, 7, 1, tzinfo=timezone.utc)

    def add(title: str, *, status: str):
        task = Task(
            title=title,
            due_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
            scheduled_date=overdue,
            created_at=overdue,
        )
        session.add(task)
        session.flush()
        session.execute(
            raw_inputs.insert(),
            {"id": uuid.uuid4(), "task_id": task.id, "status": status,
             "received_at": overdue, "source": "gmail"},
        )

    for i in range(10):
        add(f"closed-{i}", status="closed")
    add("open-overdue", status="open")
    session.commit()

    # A small limit that a naive query would exhaust on closed rows first.
    rows = tasks_store.overdue_scheduled_open(session, cutoff=cutoff, limit=3)

    titles = [t.title for t in rows]
    assert titles == ["open-overdue"]


def test_overdue_due_open_survives_limit_and_filters_closed_future():
    session = _sqlite_session()
    raw_inputs = Table(
        "raw_inputs",
        MetaData(),
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("task_id", UUID(as_uuid=True)),
        Column("status", String(32)),
        Column("received_at", DateTime(timezone=True)),
        Column("source", String(64)),
    )
    raw_inputs.create(session.get_bind())
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cutoff = base + timedelta(hours=4)

    def add(title: str, *, status: str, due: datetime):
        task = Task(
            title=title,
            due_date=due,
            scheduled_date=base,
            created_at=base,
        )
        session.add(task)
        session.flush()
        session.execute(
            raw_inputs.insert(),
            {
                "id": uuid.uuid4(),
                "task_id": task.id,
                "status": status,
                "received_at": base,
                "source": "gmail",
            },
        )

    for i in range(10):
        add(f"closed-{i}", status="closed", due=base + timedelta(minutes=i))
    add("open-overdue", status="open", due=base + timedelta(hours=1))
    add("open-future", status="open", due=base + timedelta(days=1))
    session.commit()

    rows = tasks_store.overdue_due_open(session, cutoff=cutoff, limit=3)

    assert [t.title for t in rows] == ["open-overdue"]


def test_discover_syncs_moved_managed_task_event():
    session = _sqlite_session()
    task = Task(
        title="Write report",
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        calendar_event_id="event-1",
    )
    session.add(task)
    session.commit()

    moved_start = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
    synced_task_id = discover._sync_task_schedule_from_event(
        session,
        _event("event-1", moved_start, moved_start + timedelta(minutes=30), task.id),
    )

    session.commit()
    session.refresh(task)
    assert synced_task_id == task.id
    assert task.scheduled_date == moved_start.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_add_task_event_persists_scheduled_date_after_calendar_create(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        title="Write report",
        description=None,
        link=None,
        location=None,
        label=None,
        calendar_event_id=None,
        scheduled_date=None,
    )
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)

    class DummySession:
        def __init__(self):
            self.flushed = False
            self.committed = False

        def flush(self):
            self.flushed = True

        def commit(self):
            self.committed = True

    session = DummySession()

    async def fake_create_event(_session, **kwargs):
        assert kwargs["start"] == start
        assert kwargs["end"] == end
        return _event("event-1", start, end, task.id)

    monkeypatch.setattr(
        calendar_events,
        "get_settings",
        lambda: SimpleNamespace(google_calendar_id="primary", reminder_lead_minutes=15),
    )
    monkeypatch.setattr(calendar_events, "create_event", fake_create_event)

    await calendar_events.add_task_event(session, task, start=start, end=end)

    assert task.calendar_event_id == "event-1"
    assert task.scheduled_date == start
    assert session.flushed is True
    assert session.committed is True


@pytest.mark.asyncio
async def test_plan_task_slot_tries_extended_window_automatically(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        title="Write report",
        due_date=datetime.now(timezone.utc) + timedelta(days=2),
        scheduled_date=None,
        calendar_event_id=None,
        estimation=30,
    )
    # A realistic in-window local slot (noon tomorrow) — the day-window
    # invariant in _finalize rejects physically-impossible times.
    from app.timezones import user_tz

    extended_start = (datetime.now(user_tz()) + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    extended_slot = (extended_start, extended_start + timedelta(minutes=30))
    searches: list[bool] = []
    repairs: list[bool] = []

    def fake_find_free_slot(*_args, extended_window: bool = False):
        searches.append(extended_window)
        return extended_slot if extended_window else None

    async def fake_repair_by_displacing_task(*_args, extended_window: bool, **_kwargs):
        repairs.append(extended_window)
        raise ValueError("no normal slot")

    monkeypatch.setattr(
        schedule_service,
        "get_settings",
        lambda: SimpleNamespace(
            commute_event_buffer_minutes=0,
            event_buffer_minutes=0,
            google_calendar_default_event_minutes=30,
            google_calendar_id="",
            google_busy_calendar_ids=[],
            slot_min_lead_minutes=0,
            commute_enabled=False,
            google_maps_api_key="",
        ),
    )
    monkeypatch.setattr(schedule_service, "_find_free_slot", fake_find_free_slot)
    monkeypatch.setattr(
        schedule_service,
        "_repair_by_displacing_task",
        fake_repair_by_displacing_task,
    )
    monkeypatch.setattr(
        schedule_service, "_db_scheduled_busy", lambda session, task, ws, we, busy: busy
    )

    result = await schedule_service.plan_task_slot(SimpleNamespace(), task)

    assert (result.start, result.end) == extended_slot
    assert (result.block_start, result.block_end) == extended_slot
    assert searches == [False, True]
    assert repairs == [False]


@pytest.mark.asyncio
async def test_notification_reschedule_blocks_previous_slot(monkeypatch):
    task_id = uuid.uuid4()
    scheduled = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    task = SimpleNamespace(
        id=task_id,
        title="Write report",
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=scheduled,
        calendar_event_id="event-1",
        estimation=45,
    )
    calls: list[Interval | None] = []

    async def fake_schedule_task(_session, _task, **kwargs):
        calls.append(kwargs.get("block"))
        return (scheduled + timedelta(hours=2), scheduled + timedelta(hours=2, minutes=45))

    monkeypatch.setattr(notifications.tasks_store, "get", lambda _session, _task_id: task)
    monkeypatch.setattr(notifications, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(notifications, "publish_task", lambda _session, _task_id: None)
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(home_assistant_action_secret="", google_calendar_default_event_minutes=30),
    )

    request = SimpleNamespace(headers={}, query_params={})
    payload = notifications.ActionPayload(
        action=notifications.ACTION_RESCHEDULE_TASK,
        tag=f"task-{task_id}",
    )

    result = await notifications.handle_action(payload, request, session=SimpleNamespace())

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0] == Interval(scheduled, scheduled + timedelta(minutes=45), "event-1")


def test_schedule_day_action_captures_utc_timestamp_and_queues_worker(monkeypatch):
    action_at = datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)
    background_tasks = SimpleNamespace(tasks=[])

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return action_at

    def add_task(func, *args, **kwargs):
        background_tasks.tasks.append((func, args, kwargs))

    background_tasks.add_task = add_task
    monkeypatch.setattr(home_assistant_service, "datetime", FrozenDatetime)

    queued_at = home_assistant_service.schedule_day_action(background_tasks)

    assert queued_at is action_at
    assert background_tasks.tasks == [
        (home_assistant_service.process_day_action, (action_at,), {})
    ]


@pytest.mark.asyncio
async def test_day_action_dispatches_to_home_assistant_service(monkeypatch):
    request_session = SimpleNamespace()
    background_tasks = SimpleNamespace()
    calls = []

    def fake_schedule_day_action(background_tasks_arg):
        calls.append(background_tasks_arg)

    monkeypatch.setattr(notifications, "schedule_day_action", fake_schedule_day_action)
    monkeypatch.setattr(
        notifications.tasks_store,
        "get",
        lambda *_args: pytest.fail("DAY action must not resolve a task"),
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(home_assistant_action_secret=""),
    )

    request = SimpleNamespace(headers={}, query_params={})
    payload = notifications.ActionPayload(action=notifications.ACTION_DAY)

    result = await notifications.handle_action(
        payload,
        request,
        background_tasks=background_tasks,
        session=request_session,
    )

    assert result == {
        "ok": True,
        "action": "DAY",
        "queued": True,
    }
    assert calls == [background_tasks]


@pytest.mark.asyncio
async def test_process_day_action_deducts_awake_minutes_from_points(monkeypatch):
    action_at = datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)
    background_session = SimpleNamespace(closed=False)
    adjustments: list[tuple] = []
    sleeps: list[int] = []
    events: list[str] = []

    def close_session():
        background_session.closed = True

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        events.append("sleep")

    async def fake_request_awake_minutes(session_arg, *, now):
        events.append("request")
        assert session_arg is background_session
        assert now == action_at
        return 92

    background_session.close = close_session
    monkeypatch.setattr(home_assistant_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(home_assistant_service, "SessionLocal", lambda: background_session)
    monkeypatch.setattr(
        home_assistant_service, "request_awake_minutes", fake_request_awake_minutes
    )
    monkeypatch.setattr(
        home_assistant_service,
        "adjust_points",
        lambda _session, amount, *, caller, reason: adjustments.append((amount, caller, reason)),
    )

    await home_assistant_service.process_day_action(action_at)

    assert sleeps == [home_assistant_service.DAY_HEALTH_SYNC_DELAY_S]
    assert events == ["sleep", "request"]
    assert adjustments == [(-92, "day", "awake 92 min")]
    assert background_session.closed is True


@pytest.mark.asyncio
async def test_process_day_action_without_sleep_deducts_nothing(monkeypatch):
    background_session = SimpleNamespace(closed=False)

    def close_session():
        background_session.closed = True

    async def fake_sleep(_seconds):
        return None

    async def fake_request_awake_minutes(_session, *, now):
        assert now.tzinfo is timezone.utc
        return 0

    background_session.close = close_session
    monkeypatch.setattr(home_assistant_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(home_assistant_service, "SessionLocal", lambda: background_session)
    monkeypatch.setattr(
        home_assistant_service, "request_awake_minutes", fake_request_awake_minutes
    )
    monkeypatch.setattr(
        home_assistant_service,
        "adjust_points",
        lambda *_a, **_k: pytest.fail("no sleep must not touch points"),
    )

    await home_assistant_service.process_day_action(datetime(2026, 7, 1, 6, 30))

    assert background_session.closed is True


@pytest.mark.asyncio
async def test_night_action_docks_sleep_deficit_against_8h_target(monkeypatch):
    session = SimpleNamespace()
    adjustments: list[tuple] = []

    # 7h54m until prep → 8h − 7h54m = 6 min short → round(6/2) = 3 points.
    async def fake_minutes_until_next_event_prep():
        return 474

    monkeypatch.setattr(
        notifications, "minutes_until_next_event_prep", fake_minutes_until_next_event_prep
    )
    monkeypatch.setattr(
        notifications,
        "adjust_points",
        lambda _session, amount, *, caller, reason: adjustments.append((amount, caller, reason)),
    )
    monkeypatch.setattr(
        notifications.tasks_store,
        "get",
        lambda *_args: pytest.fail("NIGHT action must not resolve a task"),
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(home_assistant_action_secret=""),
    )

    request = SimpleNamespace(headers={}, query_params={})
    payload = notifications.ActionPayload(action=notifications.ACTION_NIGHT)

    result = await notifications.handle_action(payload, request, session=session)

    assert result == {
        "ok": True,
        "action": "NIGHT",
        "minutes_until_prep": 474,
        "points_deducted": 3,
    }
    assert adjustments == [(-3, "night", "6 min under 8h")]


@pytest.mark.asyncio
async def test_night_action_deducts_nothing_with_enough_sleep(monkeypatch):
    # 8h30m until prep is over the 8h target → no deficit, no deduction.
    async def fake_minutes_until_next_event_prep():
        return 510

    monkeypatch.setattr(
        notifications, "minutes_until_next_event_prep", fake_minutes_until_next_event_prep
    )
    monkeypatch.setattr(
        notifications,
        "adjust_points",
        lambda *_a, **_k: pytest.fail("no deficit must not touch points"),
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(home_assistant_action_secret=""),
    )

    request = SimpleNamespace(headers={}, query_params={})
    payload = notifications.ActionPayload(action=notifications.ACTION_NIGHT)

    result = await notifications.handle_action(payload, request, session=SimpleNamespace())

    assert result == {
        "ok": True,
        "action": "NIGHT",
        "minutes_until_prep": 510,
        "points_deducted": 0,
    }


@pytest.mark.asyncio
async def test_night_action_skips_points_when_next_event_unknown(monkeypatch):
    async def fake_minutes_until_next_event_prep():
        return None

    monkeypatch.setattr(
        notifications, "minutes_until_next_event_prep", fake_minutes_until_next_event_prep
    )
    monkeypatch.setattr(
        notifications,
        "adjust_points",
        lambda *_a, **_k: pytest.fail("unknown next event must not touch points"),
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(home_assistant_action_secret=""),
    )

    request = SimpleNamespace(headers={}, query_params={})
    payload = notifications.ActionPayload(action=notifications.ACTION_NIGHT)

    result = await notifications.handle_action(payload, request, session=SimpleNamespace())

    assert result == {
        "ok": True,
        "action": "NIGHT",
        "minutes_until_prep": None,
        "points_deducted": 0,
    }


@pytest.mark.asyncio
async def test_task_notifications_deep_link_and_actions(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        title="Write report",
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        estimation=45,
    )
    calls: list[dict] = []

    async def fake_notify(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        notify_service,
        "get_settings",
        lambda: SimpleNamespace(task_default_url="https://example.test/app"),
    )
    monkeypatch.setattr(notify_service, "notify", fake_notify)

    await notify_service.notify_task_created(
        task,
        start=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 1, 10, 45, tzinfo=timezone.utc),
    )
    await notify_service.notify_no_slot(task)

    created, warning = calls
    # Both deep-link to the task's modal anchor.
    assert created["url"] == f"https://example.test/app#task/{task_id}"
    assert warning["url"] == f"https://example.test/app#task/{task_id}"

    # "Task created" keeps all three buttons.
    assert created["actions"] == [
        {"action": notify_service.ACTION_CLOSE_TASK, "title": "Done"},
        {"action": notify_service.ACTION_DISMISS_TASK, "title": "Dismiss"},
        {"action": notify_service.ACTION_RESCHEDULE_TASK, "title": "Reschedule"},
    ]
    # The escalated warning drops Reschedule and is undismissable.
    assert warning["actions"] == [
        {"action": notify_service.ACTION_CLOSE_TASK, "title": "Done"},
        {"action": notify_service.ACTION_DISMISS_TASK, "title": "Dismiss"},
    ]
    assert warning["persistent"] is True
    assert warning["importance"] == "high"


@pytest.mark.asyncio
async def test_points_penalty_notification_uses_points_tag(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, title="Write report")
    calls: list[dict] = []

    async def fake_notify(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        notify_service,
        "get_settings",
        lambda: SimpleNamespace(task_default_url="https://example.test/app"),
    )
    monkeypatch.setattr(notify_service, "notify", fake_notify)

    await notify_service.notify_points_penalty(
        task,
        points=10,
        reason="task is past due",
    )

    assert calls[0]["tag"] == "points"
    assert calls[0]["url"] == f"https://example.test/app#task/{task_id}"
    assert calls[0]["message"].startswith("-10 points: task is past due")


@pytest.mark.asyncio
async def test_notification_done_and_dismiss_callbacks_use_task_services(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, title="Write report")
    closed: list[uuid.UUID] = []
    dismissed: list[uuid.UUID] = []

    async def fake_close_task(_session, task_id_arg):
        closed.append(task_id_arg)

    async def fake_dismiss_task(_session, task_id_arg):
        dismissed.append(task_id_arg)

    monkeypatch.setattr(notifications.tasks_store, "get", lambda _session, _task_id: task)
    monkeypatch.setattr(notifications, "close_task_svc", fake_close_task)
    monkeypatch.setattr(notifications, "dismiss_task", fake_dismiss_task)
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: SimpleNamespace(home_assistant_action_secret=""),
    )

    request = SimpleNamespace(headers={}, query_params={})

    close_result = await notifications.handle_action(
        notifications.ActionPayload(
            action=notifications.ACTION_CLOSE_TASK,
            tag=f"task-{task_id}",
        ),
        request,
        session=SimpleNamespace(),
    )
    dismiss_result = await notifications.handle_action(
        notifications.ActionPayload(
            action=notifications.ACTION_DISMISS_TASK,
            tag=f"task-{task_id}",
        ),
        request,
        session=SimpleNamespace(),
    )

    assert close_result["ok"] is True
    assert dismiss_result["ok"] is True
    assert closed == [task_id]
    assert dismissed == [task_id]


@pytest.mark.asyncio
async def test_overdue_scheduled_cron_reschedules_with_previous_slot_blocked(monkeypatch):
    task_id = uuid.uuid4()
    scheduled = datetime.now(timezone.utc) - timedelta(minutes=50)
    task = SimpleNamespace(
        id=task_id,
        title="Write report",
        due_date=datetime.now(timezone.utc) + timedelta(days=1),
        scheduled_date=scheduled,
        calendar_event_id="event-1",
        estimation=30,
    )
    session = _sqlite_points_session()
    calls: list[Interval | None] = []
    published_points: list[float] = []
    notified: list[tuple[uuid.UUID, int, str]] = []
    published_tasks: list[uuid.UUID] = []

    @contextmanager
    def fake_session_local():
        yield session

    async def fake_schedule_task(_session, _task, **kwargs):
        calls.append(kwargs.get("block"))
        _task.scheduled_date = scheduled + timedelta(hours=1)
        return (scheduled + timedelta(hours=1), scheduled + timedelta(hours=1, minutes=30))

    async def fake_notify(task_arg, *, points, reason):
        notified.append((task_arg.id, points, reason))

    monkeypatch.setattr(cron, "SessionLocal", fake_session_local)
    monkeypatch.setattr(cron.tasks_store, "overdue_scheduled_open", lambda _session, *, cutoff: [task])
    monkeypatch.setattr(cron, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(cron, "publish_task", lambda _session, task_id_arg: published_tasks.append(task_id_arg))
    monkeypatch.setattr(cron, "publish_points", lambda session_arg: published_points.append(points_store.total(session_arg)))
    monkeypatch.setattr(cron, "notify_points_penalty", fake_notify)

    summary = await cron.reschedule_overdue_scheduled_tasks_once()

    assert summary == {"attempted": 1, "rescheduled": 1, "points_subtracted": 10}
    assert calls == [Interval(scheduled, scheduled + timedelta(minutes=30), "event-1")]
    assert points_store.total(session) == -10
    assert published_points == [-10]
    assert notified == [(task_id, 10, "scheduled date was overdue")]
    assert published_tasks == [task_id]


@pytest.mark.asyncio
async def test_overdue_scheduled_cron_waits_until_frame_and_grace_elapsed(monkeypatch):
    task_id = uuid.uuid4()
    scheduled = datetime.now(timezone.utc) - timedelta(minutes=20)
    task = SimpleNamespace(
        id=task_id,
        title="Write report",
        due_date=datetime.now(timezone.utc) + timedelta(days=1),
        scheduled_date=scheduled,
        calendar_event_id="event-1",
        estimation=30,
    )
    session = _sqlite_points_session()
    calls: list[uuid.UUID] = []
    published_points: list[float] = []
    notified: list[int] = []
    published_tasks: list[uuid.UUID] = []

    @contextmanager
    def fake_session_local():
        yield session

    async def fake_schedule_task(_session, _task, **_kwargs):
        calls.append(_task.id)
        return (scheduled + timedelta(hours=1), scheduled + timedelta(hours=1, minutes=30))

    async def fake_notify(*_args, **_kwargs):
        notified.append(1)

    monkeypatch.setattr(cron, "SessionLocal", fake_session_local)
    monkeypatch.setattr(cron.tasks_store, "overdue_scheduled_open", lambda _session, *, cutoff: [task])
    monkeypatch.setattr(cron, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(cron, "publish_task", lambda _session, task_id_arg: published_tasks.append(task_id_arg))
    monkeypatch.setattr(cron, "publish_points", lambda session_arg: published_points.append(points_store.total(session_arg)))
    monkeypatch.setattr(cron, "notify_points_penalty", fake_notify)

    summary = await cron.reschedule_overdue_scheduled_tasks_once()

    assert summary == {"attempted": 0, "rescheduled": 0, "points_subtracted": 0}
    assert calls == []
    assert points_store.total(session) == 0
    assert published_points == []
    assert notified == []
    assert published_tasks == []


@pytest.mark.asyncio
async def test_overdue_scheduled_cron_penalizes_even_failed_reschedule(monkeypatch):
    # The slot was missed whether or not a new one was found — the penalty
    # applies either way; the cleared task moves on to the retry sweep.
    task = SimpleNamespace(
        id=uuid.uuid4(),
        title="Write report",
        due_date=datetime.now(timezone.utc) + timedelta(days=1),
        scheduled_date=datetime.now(timezone.utc) - timedelta(minutes=50),
        calendar_event_id="event-1",
        estimation=30,
    )
    session = _sqlite_points_session()
    published_points: list[float] = []
    notified: list[int] = []

    @contextmanager
    def fake_session_local():
        yield session

    async def fake_schedule_task(_session, _task, **_kwargs):
        return None

    async def fake_notify(*_args, **_kwargs):
        notified.append(1)

    monkeypatch.setattr(cron, "SessionLocal", fake_session_local)
    monkeypatch.setattr(cron.tasks_store, "overdue_scheduled_open", lambda _session, *, cutoff: [task])
    monkeypatch.setattr(cron, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(cron, "publish_task", lambda _session, _task_id: None)
    monkeypatch.setattr(cron, "publish_points", lambda session_arg: published_points.append(points_store.total(session_arg)))
    monkeypatch.setattr(cron, "notify_points_penalty", fake_notify)

    summary = await cron.reschedule_overdue_scheduled_tasks_once()

    assert summary == {
        "attempted": 1,
        "rescheduled": 0,
        "points_subtracted": 10,
    }
    assert points_store.total(session) == -10
    assert published_points == [-10]
    assert notified == [1]


@pytest.mark.asyncio
async def test_overdue_due_cron_subtracts_full_hours_idempotently(monkeypatch):
    due = datetime.now(timezone.utc) - timedelta(hours=2, minutes=30)
    task = SimpleNamespace(id=uuid.uuid4(), title="Write report", due_date=due)
    session = _sqlite_points_session()
    published_points: list[float] = []
    notified: list[tuple[uuid.UUID, int, str]] = []

    @contextmanager
    def fake_session_local():
        yield session

    async def fake_notify(task_arg, *, points, reason):
        notified.append((task_arg.id, points, reason))

    monkeypatch.setattr(cron, "SessionLocal", fake_session_local)
    monkeypatch.setattr(cron.tasks_store, "overdue_due_open", lambda _session, *, cutoff: [task])
    monkeypatch.setattr(cron, "publish_points", lambda session_arg: published_points.append(points_store.total(session_arg)))
    monkeypatch.setattr(cron, "notify_points_penalty", fake_notify)

    first = await cron.penalize_overdue_due_tasks_once()
    second = await cron.penalize_overdue_due_tasks_once()

    assert first == {"checked": 1, "penalized": 1, "points_subtracted": 30}
    assert second == {"checked": 1, "penalized": 0, "points_subtracted": 0}
    assert points_store.total(session) == -30
    assert published_points == [-30]
    assert notified == [(task.id, 30, "task is past due")]


def test_discover_syncs_edited_fields_from_event():
    session = _sqlite_session()
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    task = Task(
        title="Write report",
        description="Original body",
        link="https://example.com/pr/1",
        location="Office A",
        estimation=30,
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=start,
        calendar_event_id="event-1",
    )
    session.add(task)
    session.commit()

    event = CalendarEvent(
        id="event-1",
        calendar_id="primary",
        summary="Write the Q3 report",
        # Body edited, but the trailing link left intact.
        description="Rewritten body\n\nhttps://example.com/pr/1",
        start=start,
        end=start + timedelta(minutes=60),
        all_day=False,
        location="Room 42",
        html_link=None,
        private_properties={
            "managed_by": "plan_service",
            "kind": "task",
            "task_id": str(task.id),
        },
        raw={"summary": "Write the Q3 report"},
    )

    synced = discover._sync_task_schedule_from_event(session, event)
    session.commit()
    session.refresh(task)

    assert synced == task.id
    assert task.title == "Write the Q3 report"
    assert task.location == "Room 42"
    assert task.estimation == 60
    # Unedited trailing link stripped back out of the description.
    assert task.description == "Rewritten body"


def test_discover_ignores_event_matching_pushed_state():
    session = _sqlite_session()
    start = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    task = Task(
        title="Write report",
        description="Body",
        link=None,
        location="Office A",
        estimation=45,
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=start,
        calendar_event_id="event-1",
    )
    session.add(task)
    # flush, not commit: a commit would expire the row and sqlite reloads
    # scheduled_date as naive, faking a change that postgres wouldn't see.
    session.flush()

    # Mirrors exactly what we last pushed — no field should register as changed.
    event = CalendarEvent(
        id="event-1",
        calendar_id="primary",
        summary="Write report",
        description="Body",
        start=start,
        end=start + timedelta(minutes=45),
        all_day=False,
        location="Office A",
        html_link=None,
        private_properties={
            "managed_by": "plan_service",
            "kind": "task",
            "task_id": str(task.id),
        },
        raw={"summary": "Write report"},
    )

    assert discover._sync_task_schedule_from_event(session, event) is None


@pytest.mark.asyncio
async def test_discover_reschedules_deleted_task_event_blocking_old_slot(monkeypatch):
    session = _sqlite_session()
    scheduled = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    task = Task(
        title="Write report",
        due_date=datetime(2026, 7, 2, tzinfo=timezone.utc),
        scheduled_date=scheduled,
        estimation=30,
        calendar_event_id="event-1",
    )
    session.add(task)
    session.commit()

    blocks: list[Interval | None] = []

    async def fake_schedule_task(_session, _task, **kwargs):
        blocks.append(kwargs.get("block"))
        return (scheduled + timedelta(hours=2), scheduled + timedelta(hours=2, minutes=30))

    monkeypatch.setattr(discover, "schedule_task", fake_schedule_task)

    result = await discover._reschedule_deleted_task_event(session, "event-1", account_key=None)
    session.commit()
    session.refresh(task)

    assert result == task.id
    # Old slot handed to the planner as a blocker so it isn't re-picked.
    assert blocks == [Interval(scheduled, scheduled + timedelta(minutes=30), "event-1")]
    # Dead event id cleared before replanning.
    assert task.calendar_event_id is None


@pytest.mark.asyncio
async def test_schedule_task_serializes_across_tasks(monkeypatch):
    # Two different tasks scheduled concurrently must not overlap. The global
    # lock forces the second plan to run only after the first has been written,
    # so it sees the first task's slot. Without the lock both would plan against
    # the same empty snapshot and collide.
    placed: list[tuple[datetime, datetime]] = []
    base = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)

    async def fake_plan(_session, _task, **_kwargs):
        await asyncio.sleep(0)  # yield — invites a concurrent call to interleave
        start = placed[-1][1] if placed else base
        end = start + timedelta(minutes=60)
        return schedule_service.PlannedSlot(start, end, start, end)

    async def fake_add(_session, task, *, start, end):
        await asyncio.sleep(0)
        placed.append((start, end))
        task.calendar_event_id = f"ev-{task.id}"

    monkeypatch.setattr(schedule_service, "plan_task_slot", fake_plan)
    monkeypatch.setattr("app.services.calendar.add_task_event", fake_add)

    def make_task():
        return SimpleNamespace(
            id=uuid.uuid4(),
            title="t",
            due_date=base + timedelta(days=1),
            scheduled_date=None,
            calendar_event_id=None,
            location=None,
            estimation=60,
        )

    res_a, res_b = await asyncio.gather(
        schedule_service.schedule_task(SimpleNamespace(), make_task(), notify=False),
        schedule_service.schedule_task(SimpleNamespace(), make_task(), notify=False),
    )

    (start_a, end_a), (start_b, _end_b) = sorted([res_a, res_b])
    assert end_a <= start_b  # back-to-back, no overlap
    assert len(placed) == 2
