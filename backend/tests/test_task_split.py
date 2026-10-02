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


def test_deadlines_follow_order_and_later_durations():
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    plans = [
        split.SubtaskPlan("First", None, 20),
        split.SubtaskPlan("Second", None, 50),
        split.SubtaskPlan("Third", None, 35),
    ]
    assert split._deadlines(due, plans) == [
        due - timedelta(minutes=115),
        due - timedelta(minutes=50),
        due,
    ]


def test_reflow_reorders_using_durations_and_updates_parent_sum(monkeypatch):
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    parent = SimpleNamespace(id=uuid.uuid4(), due_date=due, estimation=0)
    first = SimpleNamespace(id=uuid.uuid4(), estimation=20)
    second = SimpleNamespace(id=uuid.uuid4(), estimation=50)
    third = SimpleNamespace(id=uuid.uuid4(), estimation=35)
    published = []
    monkeypatch.setattr(split, "publish_task", lambda _s, id: published.append(id))
    session = SimpleNamespace(commit=lambda: None)

    split.reflow_children(session, parent, [third, first, second])

    assert [third.subtask_order, first.subtask_order, second.subtask_order] == [0, 1, 2]
    assert [third.due_date, first.due_date, second.due_date] == [
        due - timedelta(minutes=100), due - timedelta(minutes=65), due,
    ]
    assert parent.estimation == 105
    assert first.depends_on_task_id == third.id
    assert second.depends_on_task_id == first.id
    assert published == [parent.id, third.id, first.id, second.id]


@pytest.mark.asyncio
async def test_parent_due_date_edit_reflows_children(monkeypatch):
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    parent = SimpleNamespace(id=uuid.uuid4(), due_date=due, is_container=True, parent_task_id=None)
    first = SimpleNamespace(id=uuid.uuid4(), estimation=20, parent_task_id=parent.id, is_container=False)
    last = SimpleNamespace(id=uuid.uuid4(), estimation=50, parent_task_id=parent.id, is_container=False)
    rows = {row.id: row for row in (parent, first, last)}
    monkeypatch.setattr(task_update.tasks_store, "get", lambda _s, id: rows[id])
    monkeypatch.setattr(task_update.tasks_store, "update", lambda _s, id, **fields: _assign(rows[id], fields))
    monkeypatch.setattr(task_update.tasks_store, "children", lambda _s, _id: [first, last])
    monkeypatch.setattr(split, "publish_task", lambda *_args: None)
    monkeypatch.setattr(task_update, "publish_task", lambda *_args: None)

    requested = due + timedelta(days=1)
    await task_update.update_task(SimpleNamespace(commit=lambda: None), parent.id,
                                  {"due_date": requested}, sync_calendar=False)
    assert last.due_date == requested
    assert first.due_date == requested - timedelta(minutes=65)
    assert parent.estimation == 70


@pytest.mark.asyncio
async def test_child_duration_edit_moves_previous_deadline(monkeypatch):
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    parent = SimpleNamespace(id=uuid.uuid4(), due_date=due, is_container=True, parent_task_id=None)
    first = SimpleNamespace(id=uuid.uuid4(), estimation=20, parent_task_id=parent.id, is_container=False)
    last = SimpleNamespace(id=uuid.uuid4(), estimation=50, parent_task_id=parent.id, is_container=False)
    rows = {row.id: row for row in (parent, first, last)}
    monkeypatch.setattr(task_update.tasks_store, "get", lambda _s, id: rows[id])
    monkeypatch.setattr(task_update.tasks_store, "update", lambda _s, id, **fields: _assign(rows[id], fields))
    monkeypatch.setattr(task_update.tasks_store, "children", lambda _s, _id: [first, last])
    monkeypatch.setattr(split, "publish_task", lambda *_args: None)
    monkeypatch.setattr(task_update, "publish_task", lambda *_args: None)
    session = SimpleNamespace(commit=lambda: None)
    await task_update.update_task(session, last.id, {"estimation": 80}, sync_calendar=False)
    assert first.due_date == due - timedelta(minutes=95)
    assert last.due_date == due
    assert parent.estimation == 100
    with pytest.raises(ValueError, match="Subtask due dates"):
        await task_update.update_task(session, first.id, {"due_date": due})


def _assign(row, fields):
    for key, value in fields.items():
        setattr(row, key, value)
    return row


@pytest.mark.asyncio
async def test_manual_duration_edit_does_not_auto_split(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(), title="Long task", estimation=90,
        is_container=False, parent_task_id=None,
    )
    called = []
    async def fake_split(*_args):
        called.append(True)
        return []
    monkeypatch.setattr(task_update.tasks_store, "get", lambda *_args: task)
    monkeypatch.setattr(task_update.tasks_store, "update", lambda _s, _id, **fields: _assign(task, fields))
    monkeypatch.setattr(task_update, "publish_task", lambda *_args: None)
    monkeypatch.setattr(split, "maybe_split_task", fake_split)
    await task_update.update_task(
        SimpleNamespace(commit=lambda: None), task.id,
        {"estimation": 240}, sync_calendar=False, auto_split=False,
    )
    assert task.estimation == 240
    assert called == []


@pytest.mark.parametrize("duration, expected", [
    (35, [18, 17]),
    (125, [42, 42, 41]),
])
def test_roman_and_deterministic_piece_lengths(monkeypatch, duration, expected):
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    parent = SimpleNamespace(
        id=uuid.uuid4(), title="Draft", description="Details", link="https://example.com",
        location="Office", label="Work", estimation=duration, due_date=due,
        is_container=False, parent_task_id=None, scheduled_date=due,
        related_event_id=None, related_event_calendar_id=None,
        related_event_due_derived=False,
    )
    created = []
    def fake_create(_session, payload):
        child = SimpleNamespace(id=uuid.uuid4(), **payload.model_dump())
        created.append(child)
        return child
    monkeypatch.setattr(split.tasks_store, "create", fake_create)
    monkeypatch.setattr(split.tasks_store, "children", lambda *_args: [])
    monkeypatch.setattr(split, "publish_task", lambda *_args: None)
    session = SimpleNamespace(add=lambda *_args: None, commit=lambda: None)
    children = split.split_deterministically(session, parent)
    assert [c.title for c in children] == ["Draft I", "Draft II", "Draft III"][:len(expected)]
    assert [c.estimation for c in children] == expected
    assert children[0].due_date == due - timedelta(minutes=sum(expected[1:]) + 15 * (len(expected) - 1))
    assert all(c.description == "Details" and c.location == "Office" for c in children)
    assert parent.estimation == duration
    assert split.roman(14) == "XIV"
