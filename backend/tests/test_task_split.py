from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.task import split  # noqa: E402
from app.services.task import update as task_update  # noqa: E402


@pytest.mark.asyncio
async def test_short_task_can_decline_split(monkeypatch):
    task = SimpleNamespace(
        title="Email the proposal", description=None, estimation=30,
        due_date=datetime(2026, 10, 2, tzinfo=timezone.utc),
        related_event_calendar_id=None, related_event_id=None,
    )
    seen = {}

    async def fake_chat(*_args, **kwargs):
        seen["prompt"] = kwargs["system_prompt"]
        return SimpleNamespace(tool_calls=[SimpleNamespace(
            name="propose_subtasks", input={"subtasks": []},
        )])

    monkeypatch.setattr(split, "chat", fake_chat)
    plans = await split.propose_subtasks(task)

    assert plans == []
    assert "only separate actions explicitly stated" in seen["prompt"]


@pytest.mark.asyncio
async def test_split_step_requires_parent_evidence(monkeypatch):
    task = SimpleNamespace(
        title="Write proposal and email it", description=None, estimation=60,
        due_date=datetime(2026, 10, 2, tzinfo=timezone.utc),
        related_event_calendar_id=None, related_event_id=None,
    )

    async def fake_chat(*_args, **_kwargs):
        return SimpleNamespace(tool_calls=[SimpleNamespace(
            name="propose_subtasks", input={"subtasks": [
                {"title": "Write proposal", "estimation": 30, "source_excerpt": "Write proposal"},
                {"title": "Research competitors", "estimation": 30, "source_excerpt": "Research competitors"},
            ]},
        )])

    monkeypatch.setattr(split, "chat", fake_chat)
    with pytest.raises(ValueError, match="source evidence"):
        await split.propose_subtasks(task)


@pytest.mark.asyncio
async def test_split_sets_parent_deadline_to_last_child(monkeypatch):
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    parent = SimpleNamespace(
        id=uuid.uuid4(), due_date=due, title="Draft and send proposal",
        estimation=90, parent_task_id=None, calendar_event_id=None,
        scheduled_date=due, location=None, link=None, label=None, is_container=False,
    )
    plans = [
        split.SubtaskPlan("Draft proposal", None, 45, due_date=due - timedelta(days=2)),
        split.SubtaskPlan("Send proposal", None, 15, due_date=due - timedelta(days=1)),
    ]
    created = []

    def fake_create(_session, payload):
        child = SimpleNamespace(id=uuid.uuid4(), due_date=payload.due_date)
        created.append(child)
        return child

    async def fake_plan(*_args):
        return plans

    async def no_schedule(*_args):
        return None

    monkeypatch.setattr(split, "propose_subtasks", fake_plan)
    monkeypatch.setattr(split.tasks_store, "children", lambda *_args: [])
    monkeypatch.setattr(split.tasks_store, "create", fake_create)
    monkeypatch.setattr(split, "schedule_task", no_schedule)
    monkeypatch.setattr(split, "publish_task", lambda *_args: None)
    session = SimpleNamespace(add=lambda *_args: None, commit=lambda: None)

    result = await split.split_task(session, parent)

    assert result == created
    assert parent.is_container is True
    assert parent.due_date == created[-1].due_date == due - timedelta(days=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_parent", [False, True])
@pytest.mark.parametrize("days", [-1, 1])
async def test_editing_a_split_deadline_shifts_the_whole_group(monkeypatch, edit_parent, days):
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    parent = SimpleNamespace(id=uuid.uuid4(), due_date=due - timedelta(days=1), is_container=True, parent_task_id=None)
    first = SimpleNamespace(
        id=uuid.uuid4(), due_date=due - timedelta(days=2),
        is_container=False, parent_task_id=parent.id, due_date_derived=True,
    )
    second = SimpleNamespace(
        id=uuid.uuid4(), due_date=due - timedelta(days=1),
        is_container=False, parent_task_id=parent.id, due_date_derived=True,
    )
    rows = {row.id: row for row in (parent, first, second)}
    before = {row.id: row.due_date for row in rows.values()}
    commits = []

    def fake_update(_session, task_id, **fields):
        row = rows[task_id]
        for key, value in fields.items():
            setattr(row, key, value)
        if "due_date" in fields and "due_date_derived" not in fields and row.parent_task_id:
            row.due_date_derived = False
        return row

    async def no_calendar(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_update.tasks_store, "get", lambda _session, task_id: rows.get(task_id))
    monkeypatch.setattr(task_update.tasks_store, "update", fake_update)
    monkeypatch.setattr(task_update.tasks_store, "children", lambda _session, _id: [first, second])
    monkeypatch.setattr(task_update, "update_task_to_calendar", no_calendar)
    monkeypatch.setattr(task_update, "publish_task", lambda *_args: None)
    session = SimpleNamespace(commit=lambda: commits.append(True))
    edited = parent if edit_parent else first
    delta = timedelta(days=days)

    await task_update.update_task(session, edited.id, {"due_date": edited.due_date + delta})

    assert parent.due_date == before[parent.id] + delta
    assert parent.due_date == second.due_date
    assert first.due_date == before[first.id] + delta
    assert second.due_date == before[second.id] + delta
    assert first.due_date_derived is edit_parent
    assert second.due_date_derived is edit_parent
    assert len(commits) == 3


@pytest.mark.asyncio
async def test_parent_edit_anchors_to_last_child_even_if_legacy_dates_differ(monkeypatch):
    parent = SimpleNamespace(
        id=uuid.uuid4(), due_date=datetime(2026, 10, 10, tzinfo=timezone.utc),
        is_container=True, parent_task_id=None,
    )
    last = SimpleNamespace(
        id=uuid.uuid4(), due_date=datetime(2026, 10, 9, tzinfo=timezone.utc),
        is_container=False, parent_task_id=parent.id, due_date_derived=True,
    )
    rows = {parent.id: parent, last.id: last}

    def fake_update(_session, task_id, **fields):
        row = rows[task_id]
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    async def no_calendar(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_update.tasks_store, "get", lambda _session, task_id: rows[task_id])
    monkeypatch.setattr(task_update.tasks_store, "update", fake_update)
    monkeypatch.setattr(task_update.tasks_store, "children", lambda _session, _id: [last])
    monkeypatch.setattr(task_update, "update_task_to_calendar", no_calendar)
    monkeypatch.setattr(task_update, "publish_task", lambda *_args: None)

    requested = datetime(2026, 10, 12, tzinfo=timezone.utc)
    await task_update.update_task(SimpleNamespace(commit=lambda: None), parent.id, {"due_date": requested})

    assert parent.due_date == last.due_date == requested
