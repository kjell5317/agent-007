from __future__ import annotations

import contextlib
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


def _planner_task(title="Write proposal and email it", description=None, estimation=60):
    return SimpleNamespace(
        title=title, description=description, estimation=estimation,
        due_date=datetime(2026, 10, 2, tzinfo=timezone.utc),
        related_event_calendar_id=None, related_event_id=None,
    )


def _planner_returns(monkeypatch, subtasks):
    async def fake_chat(*_args, **_kwargs):
        return SimpleNamespace(tool_calls=[SimpleNamespace(
            name="propose_subtasks", input={"subtasks": subtasks},
        )])
    monkeypatch.setattr(split, "chat", fake_chat)


@pytest.mark.asyncio
async def test_planner_leaves_gemini_room_to_think_and_names_truncation(monkeypatch):
    seen = {}

    async def truncated_chat(*_args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(tool_calls=[], stop_reason="length")

    monkeypatch.setattr(split, "get_settings", lambda: SimpleNamespace(
        gemini_api_key="key", chat_llm_model="gemini-test", chat_thinking_level="low",
    ))
    monkeypatch.setattr(split, "chat", truncated_chat)
    with pytest.raises(ValueError, match="token limit"):
        await split.propose_subtasks(_planner_task(estimation=180))

    assert seen["provider"] == "google"
    assert seen["thinking_level"] == "low"
    assert seen["max_tokens"] >= 4096


@pytest.mark.asyncio
async def test_after_previous_links_only_to_the_step_before(monkeypatch):
    _planner_returns(monkeypatch, [
        {"title": "Write proposal", "estimation": 30, "source_excerpt": "Write proposal", "after_previous": True},
        {"title": "Email it", "estimation": 10, "source_excerpt": "email it", "after_previous": True},
    ])
    plans = await split.propose_subtasks(_planner_task())
    assert [p.after_previous for p in plans] == [False, True]


@pytest.mark.asyncio
async def test_excerpt_matches_through_markdown_emphasis(monkeypatch):
    task = _planner_task(
        title="Fix PR review issues",
        description=(
            "1. **Same-second events ordering**: In `rules.ts:274–278`, ties\n"
            "   reorder `/factory stop`.\n2. **Code blocks**: exclude fenced stops."
        ),
        estimation=120,
    )
    _planner_returns(monkeypatch, [
        {"title": "Fix ordering", "estimation": 60,
         "source_excerpt": "Same-second events ordering: In rules.ts:274–278, ties reorder /factory stop."},
        {"title": "Skip code blocks", "estimation": 60, "source_excerpt": "Code blocks: exclude fenced stops."},
    ])
    plans = await split.propose_subtasks(task)
    assert [p.title for p in plans] == ["Fix ordering", "Skip code blocks"]


@pytest.mark.asyncio
async def test_split_task_chains_children_marked_after_previous(monkeypatch):
    parent = SimpleNamespace(
        id=uuid.uuid4(), title="Ship it", description=None, link=None, location=None,
        label=None, estimation=180, due_date=datetime(2026, 10, 10, 12, tzinfo=timezone.utc),
        parent_task_id=None, is_container=False, calendar_event_id=None,
        work_type=None, effort=None, deadline_kind=None,
    )

    async def plans(_task):
        return [
            split.SubtaskPlan("Build", None, 60),
            split.SubtaskPlan("Docs", None, 30),
            split.SubtaskPlan("Release", None, 30, after_previous=True),
        ]

    def fake_create(_session, payload):
        return SimpleNamespace(id=uuid.uuid4(), **payload.model_dump())

    @contextlib.asynccontextmanager
    async def no_batch(_session):
        yield

    async def no_schedule(*_args):
        return None

    monkeypatch.setattr(split, "propose_subtasks", plans)
    monkeypatch.setattr(split.tasks_store, "children", lambda *_args: [])
    monkeypatch.setattr(split.tasks_store, "create", fake_create)
    monkeypatch.setattr(split, "publish_task", lambda *_args: None)
    monkeypatch.setattr(split, "replan_batch", no_batch)
    monkeypatch.setattr(split, "schedule_task", no_schedule)
    session = SimpleNamespace(add=lambda *_args: None, commit=lambda: None)

    build, docs, release = await split.split_task(session, parent)

    assert build.depends_on_task_id is None
    assert docs.depends_on_task_id is None
    assert release.depends_on_task_id == docs.id


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


@pytest.mark.asyncio
async def test_closed_sibling_is_not_synced_after_child_duration_edit(monkeypatch):
    due = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    parent = SimpleNamespace(id=uuid.uuid4(), due_date=due, is_container=True, parent_task_id=None)
    closed = SimpleNamespace(id=uuid.uuid4(), estimation=20, parent_task_id=parent.id, is_container=False)
    active = SimpleNamespace(id=uuid.uuid4(), estimation=50, parent_task_id=parent.id, is_container=False)
    rows = {row.id: row for row in (parent, closed, active)}
    synced = []

    async def fake_calendar_sync(_session, row, *, changed_fields):
        synced.append((row.id, changed_fields))

    monkeypatch.setattr(task_update.tasks_store, "get", lambda _s, task_id: rows[task_id])
    monkeypatch.setattr(task_update.tasks_store, "update", lambda _s, task_id, **fields: _assign(rows[task_id], fields))
    monkeypatch.setattr(task_update.tasks_store, "children", lambda _s, _id: [closed, active])
    monkeypatch.setattr(
        task_update.tasks_store, "latest_status_for",
        lambda _s, _ids: {closed.id: "closed", active.id: "open"},
    )
    monkeypatch.setattr(task_update, "update_task_to_calendar", fake_calendar_sync)
    monkeypatch.setattr(split, "publish_task", lambda *_args: None)
    monkeypatch.setattr(task_update, "publish_task", lambda *_args: None)

    await task_update.update_task(
        SimpleNamespace(commit=lambda: None), active.id,
        {"estimation": 80}, auto_split=False,
    )

    assert synced == [(active.id, {"due_date", "estimation"})]


@pytest.mark.asyncio
async def test_parent_label_edit_relabels_subtasks_still_on_its_label(monkeypatch):
    parent = SimpleNamespace(id=uuid.uuid4(), label="Work", is_container=True, parent_task_id=None)
    follower = SimpleNamespace(id=uuid.uuid4(), label="Work", parent_task_id=parent.id, is_container=False)
    own = SimpleNamespace(id=uuid.uuid4(), label="Errands", parent_task_id=parent.id, is_container=False)
    rows = {row.id: row for row in (parent, follower, own)}
    synced, published = [], []

    async def fake_calendar_sync(_session, row, *, changed_fields):
        synced.append((row.id, changed_fields))

    monkeypatch.setattr(task_update.tasks_store, "get", lambda _s, task_id: rows[task_id])
    monkeypatch.setattr(task_update.tasks_store, "update", lambda _s, task_id, **fields: _assign(rows[task_id], fields))
    monkeypatch.setattr(task_update.tasks_store, "children", lambda _s, _id: [follower, own])
    monkeypatch.setattr(task_update.tasks_store, "latest_status_for", lambda _s, _ids: {})
    monkeypatch.setattr(task_update, "update_task_to_calendar", fake_calendar_sync)
    monkeypatch.setattr(task_update, "publish_task", lambda _s, task_id: published.append(task_id))

    await task_update.update_task(
        SimpleNamespace(commit=lambda: None), parent.id, {"label": "Study"}, auto_split=False,
    )

    assert parent.label == "Study"
    assert follower.label == "Study"
    assert own.label == "Errands"
    assert published == [parent.id, follower.id]
    assert synced == [(follower.id, {"label"})]


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
    (45, [30, 15]),
    (135, [45, 45, 45]),
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


def test_needs_split_lets_jev_replace_the_conjunction_cue():
    task = SimpleNamespace(title="Watch Tom and Jerry", estimation=30)
    assert split.needs_split(task)
    assert not split.needs_split(task, split_hint=False)
    assert split.needs_split(SimpleNamespace(title="Gym", estimation=30), split_hint=True)
    # A long task splits whatever Jev says.
    assert split.needs_split(SimpleNamespace(title="Thesis", estimation=240), split_hint=False)


@pytest.mark.asyncio
async def test_maybe_split_asks_jev_only_for_a_conjunction_cue(monkeypatch):
    asked = []

    async def hint(task):
        asked.append(task.title)
        return False

    async def never_split(session, task, *, force=False):
        raise AssertionError("Jev declined the split")

    monkeypatch.setattr(split, "jev_split_hint", hint)
    monkeypatch.setattr(split, "split_task", never_split)

    cue = SimpleNamespace(id=uuid.uuid4(), title="Watch Tom and Jerry", estimation=30, parent_task_id=None, is_container=False)
    plain = SimpleNamespace(id=uuid.uuid4(), title="Gym", estimation=30, parent_task_id=None, is_container=False)
    assert await split.maybe_split_task(None, cue) == []
    assert await split.maybe_split_task(None, plain) == []
    assert asked == ["Watch Tom and Jerry"]


@pytest.mark.asyncio
async def test_maybe_split_keeps_the_conjunction_rule_without_jev(monkeypatch):
    async def no_hint(task):
        return None

    async def fake_split(session, task, *, force=False):
        return ["child"]

    monkeypatch.setattr(split, "jev_split_hint", no_hint)
    monkeypatch.setattr(split, "split_task", fake_split)
    cue = SimpleNamespace(id=uuid.uuid4(), title="Wash and dry laundry", estimation=30, parent_task_id=None, is_container=False)
    assert await split.maybe_split_task(None, cue) == ["child"]
