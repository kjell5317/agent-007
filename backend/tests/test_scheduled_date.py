from __future__ import annotations

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
from app.db.models.task import Task  # noqa: E402
from app.db.schemas.task import TaskRead  # noqa: E402
from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.calendar import discover  # noqa: E402
from app.services.calendar import events as calendar_events  # noqa: E402
from app.services import notify as notify_service  # noqa: E402
from app.services.plan import schedule as schedule_service  # noqa: E402
from app.services.plan.schedule import Interval  # noqa: E402


def _sqlite_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Task.__table__.create(engine)
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
        estimation=30,
        location=None,
        label=None,
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
    )

    read = TaskRead.build(row, "open", True)

    assert read.scheduled_date == scheduled
    assert read.model_dump(mode="json")["scheduled_date"] == "2026-07-01T12:30:00Z"


def test_task_list_orders_by_display_date(monkeypatch):
    session = _sqlite_session()
    created = datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc)
    # Every task carries a scheduled_date now; display order follows it, and it
    # wins over due_date in the coalesce (this row's due is latest of all).
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
    late = Task(
        title="Scheduled latest",
        scheduled_date=datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        calendar_event_id="event-late",
        created_at=created + timedelta(minutes=2),
    )
    session.add_all([late, early, middle])
    session.commit()

    monkeypatch.setattr(
        tasks_store,
        "latest_status_for",
        lambda _session, ids: {task_id: "open" for task_id in ids},
    )

    rows = tasks_store.list_(session)

    assert [task.title for task, _status in rows] == [
        "Scheduled earliest",
        "Scheduled middle",
        "Scheduled latest",
    ]


def test_open_filter_survives_limit_with_many_closed(monkeypatch):
    # Regression: with display_date-ASC ordering, completed tasks (old due
    # dates) sort first. If `limit` is applied before the status filter, they
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
        # display order is deterministic (closed tasks land oldest, filling the
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
        lambda: SimpleNamespace(google_calendar_id="primary"),
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
    extended_start = datetime.now(timezone.utc) + timedelta(hours=6)
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
            google_calendar_default_event_minutes=30,
            google_calendar_id="",
            google_busy_calendar_ids=[],
        ),
    )
    monkeypatch.setattr(schedule_service, "_find_free_slot", fake_find_free_slot)
    monkeypatch.setattr(
        schedule_service,
        "_repair_by_displacing_task",
        fake_repair_by_displacing_task,
    )

    result = await schedule_service.plan_task_slot(SimpleNamespace(), task)

    assert result == extended_slot
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
    scheduled = datetime.now(timezone.utc) - timedelta(minutes=20)
    task = SimpleNamespace(
        id=task_id,
        title="Write report",
        due_date=datetime.now(timezone.utc) + timedelta(days=1),
        scheduled_date=scheduled,
        calendar_event_id="event-1",
        estimation=30,
    )
    calls: list[Interval | None] = []

    @contextmanager
    def fake_session_local():
        yield SimpleNamespace()

    async def fake_schedule_task(_session, _task, **kwargs):
        calls.append(kwargs.get("block"))
        return (scheduled + timedelta(hours=1), scheduled + timedelta(hours=1, minutes=30))

    monkeypatch.setattr(cron, "SessionLocal", fake_session_local)
    monkeypatch.setattr(cron.tasks_store, "overdue_scheduled_open", lambda _session, *, cutoff: [task])
    monkeypatch.setattr(cron, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(cron, "publish_task", lambda _session, _task_id: None)

    summary = await cron.reschedule_overdue_scheduled_tasks_once()

    assert summary == {"attempted": 1, "rescheduled": 1}
    assert calls == [Interval(scheduled, scheduled + timedelta(minutes=30), "event-1")]


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
