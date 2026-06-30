from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
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
    due_only = Task(
        title="Due-only earlier",
        due_date=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        scheduled_date=None,
        calendar_event_id="historical-event",
        created_at=created,
    )
    scheduled = Task(
        title="Scheduled later",
        due_date=datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc),
        scheduled_date=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        calendar_event_id="event-1",
        created_at=created + timedelta(minutes=1),
    )
    undated_newer = Task(
        title="Undated newer",
        created_at=created + timedelta(minutes=2),
    )
    undated_older = Task(
        title="Undated older",
        created_at=created,
    )
    session.add_all([scheduled, undated_older, due_only, undated_newer])
    session.commit()

    monkeypatch.setattr(
        tasks_store,
        "latest_status_for",
        lambda _session, ids: {task_id: "open" for task_id in ids},
    )

    rows = tasks_store.list_(session)

    assert [task.title for task, _status in rows] == [
        "Due-only earlier",
        "Scheduled later",
        "Undated newer",
        "Undated older",
    ]


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
