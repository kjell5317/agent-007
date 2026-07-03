from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.api import points as points_api  # noqa: E402
from app.db.clients import points as points_store  # noqa: E402
from app.db.models.points_entry import PointsEntry  # noqa: E402
from app.db.models.task import Task  # noqa: E402
from app.events import publish as publish_events  # noqa: E402
from app.services import points as points_service  # noqa: E402
from app.services.task import close as close_service  # noqa: E402


def _sqlite_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Task.__table__.create(engine)
    PointsEntry.__table__.create(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _task(*, estimation: int | None) -> Task:
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    return Task(
        title="Write report",
        due_date=now,
        scheduled_date=now,
        estimation=estimation,
    )


def _decode(payloads: list[str]) -> list[dict]:
    return [json.loads(payload) for payload in payloads]


def _open_access(monkeypatch) -> None:
    """Make `_check_access` a no-op regardless of the local .env — the fake
    SimpleNamespace requests carry no headers to read a notify secret from."""
    monkeypatch.setattr(
        points_api,
        "get_settings",
        lambda: SimpleNamespace(auth_allowed_emails=[], home_assistant_action_secret=""),
    )


def test_manual_adjust_publishes_new_total(monkeypatch):
    session = _sqlite_session()
    _open_access(monkeypatch)
    published: list[str] = []
    monkeypatch.setattr(publish_events.bus, "publish", published.append)

    result = points_api.adjust(
        points_api.AdjustPayload(amount=7, caller="Manual", reason="Goodwill credit"),
        SimpleNamespace(session={}),
        session=session,
    )

    assert result.total == 7
    assert points_store.total(session) == 7
    entry = session.query(PointsEntry).one()
    assert entry.section == "Manual"
    assert entry.action_name == "Goodwill credit"
    assert _decode(published) == [{"type": "points", "total": 7.0}]


def test_points_log_keeps_history_and_counts_unseen(monkeypatch):
    session = _sqlite_session()
    _open_access(monkeypatch)
    before_seen = points_store.add_entry(
        session,
        source="manual",
        section="Manual",
        action_name="Old change",
        factor=2,
        quantity=1,
        amount=2,
    )
    after_seen = points_store.add_entry(
        session,
        source="manual",
        section="Manual",
        action_name="New change",
        factor=-3,
        quantity=1,
        amount=-3,
    )
    before_seen.created_at = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    after_seen.created_at = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
    session.commit()

    checkpoint = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(points_api.state, "last_seen_points_log_at", checkpoint)

    log = points_api.get_points_log(
        SimpleNamespace(session={}),
        session=session,
    )

    assert log.count == 1
    assert log.last_seen_at == checkpoint
    # The log is a history padded to MIN_LOG_ENTRIES — seen entries stay
    # visible; only `count` reflects the watermark.
    assert [entry.id for entry in log.entries] == [after_seen.id, before_seen.id]
    assert log.entries[0].amount == -3
    assert log.entries[0].reason == "New change"
    assert log.entries[0].caller == "Manual"

    seen = points_api.mark_points_log_seen(SimpleNamespace(session={}), session=session)

    assert seen.count == 0
    assert seen.last_seen_at > checkpoint
    after = points_api.get_points_log(SimpleNamespace(session={}), session=session)
    assert after.count == 0
    assert [entry.id for entry in after.entries] == [after_seen.id, before_seen.id]


@pytest.mark.asyncio
async def test_close_task_awards_points_and_publishes_new_total(monkeypatch):
    session = _sqlite_session()
    task = _task(estimation=30)
    session.add(task)
    session.commit()

    published: list[str] = []
    published_tasks: list[uuid.UUID] = []
    published_inputs: list[uuid.UUID] = []
    raw_input_id = uuid.uuid4()
    raw_input = SimpleNamespace(id=raw_input_id, status="open", processed_at=None)

    monkeypatch.setattr(
        points_service,
        "get_settings",
        lambda: SimpleNamespace(points_task_done_factor=0.5),
    )
    monkeypatch.setattr(publish_events.bus, "publish", published.append)
    monkeypatch.setattr(close_service.raw_inputs_store, "latest_for_task", lambda *_args: raw_input)
    monkeypatch.setattr(close_service, "publish_task", lambda _session, task_id: published_tasks.append(task_id))
    monkeypatch.setattr(close_service, "publish_input", lambda _session, input_id: published_inputs.append(input_id))

    async def fake_delete_task_event(*_args, **_kwargs):
        return None

    async def fake_clear_task_notification(*_args, **_kwargs):
        return None

    monkeypatch.setattr(close_service, "delete_task_event", fake_delete_task_event)
    monkeypatch.setattr(close_service, "clear_task_notification", fake_clear_task_notification)

    await close_service.close_task(session, task.id)

    assert points_store.total(session) == 15
    assert _decode(published) == [{"type": "points", "total": 15.0}]
    assert raw_input.status == "closed"
    assert published_tasks == [task.id]
    assert published_inputs == [raw_input_id]


@pytest.mark.asyncio
async def test_close_task_without_award_does_not_publish_points(monkeypatch):
    session = _sqlite_session()
    task = _task(estimation=30)
    session.add(task)
    session.commit()
    points_store.add_entry(
        session,
        source="task",
        action_name=task.title,
        task_id=task.id,
        factor=0.5,
        quantity=30,
        amount=15,
    )

    published: list[str] = []
    raw_input = SimpleNamespace(id=uuid.uuid4(), status="open", processed_at=None)

    monkeypatch.setattr(
        points_service,
        "get_settings",
        lambda: SimpleNamespace(points_task_done_factor=0.5),
    )
    monkeypatch.setattr(publish_events.bus, "publish", published.append)
    monkeypatch.setattr(close_service.raw_inputs_store, "latest_for_task", lambda *_args: raw_input)
    monkeypatch.setattr(close_service, "publish_task", lambda *_args: None)
    monkeypatch.setattr(close_service, "publish_input", lambda *_args: None)

    async def fake_delete_task_event(*_args, **_kwargs):
        return None

    async def fake_clear_task_notification(*_args, **_kwargs):
        return None

    monkeypatch.setattr(close_service, "delete_task_event", fake_delete_task_event)
    monkeypatch.setattr(close_service, "clear_task_notification", fake_clear_task_notification)

    await close_service.close_task(session, task.id)

    assert points_store.total(session) == 15
    assert published == []
    assert raw_input.status == "closed"
