"""`replan()` end to end with the calendar, the task table and the log faked."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db.models.task import Task
from app.services.plan import features as F
from app.services.plan import replan
from app.services.plan.timeline import Item

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)  # Monday morning


def make_task(title, *, due, scheduled=None, minutes=60, **kw) -> Task:
    task = Task(
        id=uuid.uuid4(), title=title, due_date=due, scheduled_date=scheduled, estimation=minutes,
        calendar_event_id=f"ev-{title}" if scheduled else None,
        schedule_state="placed" if scheduled else "unplaced",
        deadline_kind=kw.pop("deadline_kind", "firm"), placed_by=kw.pop("placed_by", "planner"),
        is_container=False, **kw,
    )
    return task


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(tasks=[], events=[], writes=[], deletes=[], logs=[], resolved={})

    async def calendar_items(_session, _start, _end, account_key=None):
        return state.events

    async def update_task_event(_session, task, *, start, end, changed_fields=None):
        state.writes.append((task.title, start, end))
        task.scheduled_date = start
        task.calendar_event_id = task.calendar_event_id or f"ev-{task.title}"

    async def delete_task_event(_session, task):
        state.deletes.append(task.title)
        task.calendar_event_id = None

    async def no_commutes(_session, _spans):
        return None

    monkeypatch.setattr(replan, "calendar_items", calendar_items)
    monkeypatch.setattr("app.services.calendar.update_task_event", update_task_event)
    monkeypatch.setattr("app.services.calendar.delete_task_event", delete_task_event)
    monkeypatch.setattr(replan, "_replan_commutes", no_commutes)
    monkeypatch.setattr(replan.tasks_store, "open_schedulable", lambda _s: state.tasks)
    monkeypatch.setattr(replan.tasks_store, "get", lambda _s, _id: None)
    monkeypatch.setattr(replan.log_store, "by_slot_for_tasks", lambda _s, _ids, _kinds: state.resolved)
    monkeypatch.setattr(replan.log_store, "add", lambda _s, **kw: state.logs.append(kw))
    monkeypatch.setattr(replan.overrides_store, "task_ids_with_user_override", lambda _s, _f: set())
    monkeypatch.setattr("app.services.learn.jobs.weights_for", lambda _s, _d: F.prior_mean())
    monkeypatch.setattr("app.services.learn.jobs.duration_params", lambda _s: None)
    monkeypatch.setattr("app.services.learn.jobs.rho", lambda _s: 0.5)
    monkeypatch.setattr("app.events.publish_task", lambda _s, _id: None)
    return state


def session():
    return SimpleNamespace(commit=lambda: None, flush=lambda: None, rollback=lambda: None)


@pytest.mark.asyncio
async def test_replan_places_moves_and_leaves_alone(world):
    lecture = Item(NOW.replace(hour=10), NOW.replace(hour=16), "event", label="Uni", event_id="e1")
    world.events = [lecture]
    fresh = make_task("fresh", due=NOW + timedelta(days=2))
    clash = make_task("clash", due=NOW + timedelta(days=3), scheduled=NOW.replace(hour=11))
    steady = make_task("steady", due=NOW + timedelta(days=5),
                       scheduled=(NOW + timedelta(days=1)).replace(hour=14))
    waiting = make_task("waiting", due=NOW + timedelta(days=1),
                        scheduled=NOW - timedelta(hours=1, minutes=30))
    world.tasks = [fresh, clash, steady, waiting]

    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)

    assert result.ok
    written = {title: (start, end) for title, start, end in world.writes}
    assert set(written) == {"fresh", "clash"}
    for start, end in written.values():
        assert end <= lecture.start or start >= lecture.end
        assert 10 <= start.hour and end.hour <= 19 or (end.hour == 19 and end.minute == 0)
    assert steady.title not in written  # stability: no gratuitous move
    assert waiting.id not in result.placed  # check-in outcome still open
    assert all(log["kind"] == "placed" and log["features"]["slot"] for log in world.logs)
    assert fresh.schedule_state == "placed"


@pytest.mark.asyncio
async def test_running_task_blocks_its_extension(world):
    running = make_task("running", due=NOW + timedelta(days=1),
                        scheduled=NOW - timedelta(minutes=90), running_since=NOW - timedelta(minutes=10))
    # Booked 60 min ending 08:30; at 09:00 the extension runs to 09:15.
    blocked = make_task("next", due=NOW + timedelta(hours=8), scheduled=NOW.replace(hour=9, minute=15))
    world.tasks = [running, blocked]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    assert "running" not in {title for title, _, _ in world.writes}
    start, _ = result.placed[blocked.id]
    assert start >= NOW.replace(hour=9, minute=15)


@pytest.mark.asyncio
async def test_far_deadline_is_deferred_when_the_week_is_full(world):
    world.events = [Item(NOW.replace(hour=0), NOW.replace(hour=0) + timedelta(days=8), "event")]
    far = make_task("far", due=NOW + timedelta(days=60), scheduled=None)
    world.tasks = [far]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5)
    assert far.id in result.deferred
    assert far.schedule_state == "deferred"
    assert [log["kind"] for log in world.logs] == ["deferred"]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(world):
    world.tasks = [make_task("fresh", due=NOW + timedelta(days=2))]
    result = await replan.replan(session(), reason="test", now=NOW, time_limit=5, dry_run=True)
    assert result.ok and result.placed
    assert world.writes == [] and world.logs == []
