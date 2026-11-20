"""Work sessions: start, pause with a reason, complete, and logged work."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.db.models.work import WorkSession
from app.services.plan.schedule import remaining_minutes
from app.services.task import work

NOW = datetime(2026, 11, 17, 14, tzinfo=timezone.utc)


def _task(**kw):
    base = dict(
        id=uuid.uuid4(), title="Write report", estimation=60, worked_minutes=0, booked_minutes=None,
        running_since=None, waiting_since=None, scheduled_date=NOW + timedelta(hours=2),
        schedule_state="placed", calendar_event_id="ev-1", is_container=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(tasks={}, sessions=[], logs=[], pinned=[], scheduled=[], solves=[],
                        cleared=[], closed=[], deleted=[], status={}, later=[])

    def running(_s):
        return next((r for r in w.sessions if r.ended_at is None), None)

    def open_for_task(_s, task_id):
        return next((r for r in w.sessions if r.task_id == task_id and r.ended_at is None), None)

    def start(_s, task_id, at):
        row = WorkSession(id=uuid.uuid4(), task_id=task_id, started_at=at, source="live")
        w.sessions.append(row)
        return row

    def end(_s, row, at, reason):
        row.ended_at, row.end_reason = at, reason

    def add_logged(_s, task_id, started_at, ended_at, reason):
        row = WorkSession(id=uuid.uuid4(), task_id=task_id, started_at=started_at,
                          ended_at=ended_at, end_reason=reason, source="retro")
        w.sessions.append(row)
        return row

    async def pin(_s, task, *, start, minutes):
        task.scheduled_date, task.booked_minutes = start, minutes
        w.pinned.append((task.id, start, minutes))

    async def schedule(_s, task, *, block=None):
        w.scheduled.append((task.id, block))

    async def clear(_s, task_id, *, kinds=None):
        w.cleared.append((task_id, kinds))

    async def close(_s, task_id):
        w.closed.append(task_id)

    async def delete_event(_s, task):
        w.deleted.append(task.id)
        task.calendar_event_id = None

    monkeypatch.setattr(work.work_store, "running", running)
    monkeypatch.setattr(work.work_store, "open_for_task", open_for_task)
    monkeypatch.setattr(work.work_store, "start", start)
    monkeypatch.setattr(work.work_store, "end", end)
    monkeypatch.setattr(work.work_store, "add_logged", add_logged)
    monkeypatch.setattr(work.tasks_store, "get", lambda _s, task_id: w.tasks.get(task_id))
    monkeypatch.setattr(work.tasks_store, "latest_status_for",
                        lambda _s, ids: {i: w.status.get(i, "open") for i in ids})
    monkeypatch.setattr(work.log_store, "add", lambda _s, **kw: w.logs.append(kw))
    def solve_later(task_id=None, *, block=None):
        if task_id is None:
            w.solves.append("work_session")
        else:
            w.scheduled.append((task_id, block))

    monkeypatch.setattr(work, "pin_running", pin)
    monkeypatch.setattr(work, "schedule_task", schedule)
    monkeypatch.setattr(work, "_solve_later", solve_later)
    monkeypatch.setattr(work, "_later", lambda _what, step: w.later.append(step))
    monkeypatch.setattr(work, "clear_task_notifications", clear)
    monkeypatch.setattr(work, "close_task", close)
    monkeypatch.setattr(work, "publish_task", lambda *_a: None)
    monkeypatch.setattr(work, "_refit_soon", lambda: None)
    monkeypatch.setattr("app.services.calendar.delete_task_event", delete_event)
    return w


def _session():
    return SimpleNamespace(commit=lambda: None)


def _add(world, task):
    world.tasks[task.id] = task
    return task


@pytest.mark.asyncio
async def test_start_pins_the_slot_to_now_and_logs_the_planned_one(world):
    task = _add(world, _task(worked_minutes=20))
    planned = task.scheduled_date

    await work.start_task(_session(), task, now=NOW)

    assert task.running_since == NOW
    # Only what is left of the estimate is booked.
    assert world.pinned == [(task.id, NOW, 40)]
    assert world.logs[0]["kind"] == "started" and world.logs[0]["slot_start"] == planned
    assert world.solves == ["work_session"]
    # The start cue's push is cleared after the tap is answered.
    assert world.cleared == []
    for step in world.later:
        await step(_session())
    assert world.cleared == [(task.id, {"start_cue", "new_task"})]


@pytest.mark.asyncio
async def test_starting_another_task_pauses_the_running_one_as_switched(world):
    first = _add(world, _task())
    second = _add(world, _task(title="Reply to Anna"))
    await work.start_task(_session(), first, now=NOW)
    first.scheduled_date = NOW

    await work.start_task(_session(), second, now=NOW + timedelta(minutes=25))

    paused = world.sessions[0]
    assert paused.end_reason == "switched" and first.running_since is None
    assert first.worked_minutes == 25
    # The first task's remainder goes back to the planner, off its old slot.
    [(task_id, block)] = world.scheduled
    assert task_id == first.id and block.start == NOW
    assert second.running_since == NOW + timedelta(minutes=25)


@pytest.mark.asyncio
async def test_starting_a_running_task_again_changes_nothing(world):
    task = _add(world, _task())
    first = await work.start_task(_session(), task, now=NOW)
    again = await work.start_task(_session(), task, now=NOW + timedelta(minutes=5))
    assert again is first and len(world.sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("reason", "block"), [("break", 10), ("interrupted", 30)])
async def test_pause_counts_only_worked_time_and_replans_the_rest(world, reason, block):
    task = _add(world, _task())
    await work.start_task(_session(), task, now=NOW)

    await work.pause_task(_session(), task, reason, now=NOW + timedelta(minutes=35))

    assert task.worked_minutes == 35 and task.running_since is None
    assert world.sessions[0].end_reason == reason
    [(_, interval)] = world.scheduled
    assert interval.start == NOW
    assert interval.end == NOW + timedelta(minutes=35 + block)
    # A break is never task time: the next start books what's left.
    assert remaining_minutes(task, SimpleNamespace(google_calendar_default_event_minutes=30)) == 25


@pytest.mark.asyncio
async def test_waiting_takes_the_task_off_the_calendar_until_resumed(world):
    task = _add(world, _task())
    await work.start_task(_session(), task, now=NOW)

    await work.pause_task(_session(), task, "waiting", now=NOW + timedelta(minutes=10))

    assert task.schedule_state == "waiting" and task.waiting_since == NOW + timedelta(minutes=10)
    assert task.scheduled_date is None and world.deleted == [task.id]
    assert world.scheduled == [] and world.solves == ["work_session", "work_session"]

    await work.resume_task(_session(), task)
    assert task.schedule_state == "unplaced" and task.waiting_since is None
    assert world.scheduled == [(task.id, None)]


@pytest.mark.asyncio
async def test_a_task_can_wait_before_it_was_ever_started(world):
    task = _add(world, _task())

    await work.wait_task(_session(), task, now=NOW)

    assert task.schedule_state == "waiting" and task.waiting_since == NOW
    assert task.scheduled_date is None and world.deleted == [task.id]
    assert world.sessions == [] and world.solves == ["work_session"]

    # Waiting again changes nothing.
    await work.wait_task(_session(), task, now=NOW + timedelta(minutes=5))
    assert task.waiting_since == NOW and world.deleted == [task.id]


@pytest.mark.asyncio
async def test_waiting_on_a_running_task_ends_its_session(world):
    task = _add(world, _task())
    await work.start_task(_session(), task, now=NOW)

    await work.wait_task(_session(), task, now=NOW + timedelta(minutes=10))

    assert world.sessions[0].end_reason == "waiting" and task.worked_minutes == 10
    assert task.schedule_state == "waiting" and task.running_since is None


@pytest.mark.asyncio
async def test_a_closed_task_cant_wait(world):
    task = _add(world, _task())
    world.status[task.id] = "closed"
    with pytest.raises(ValueError):
        await work.wait_task(_session(), task, now=NOW)
    assert task.schedule_state == "placed"


@pytest.mark.asyncio
async def test_pausing_a_task_that_isnt_running_fails(world):
    task = _add(world, _task())
    with pytest.raises(LookupError):
        await work.pause_task(_session(), task, "break", now=NOW)
    with pytest.raises(ValueError):
        await work.pause_task(_session(), task, "lunch", now=NOW)


@pytest.mark.asyncio
async def test_complete_ends_the_session_and_closes(world):
    task = _add(world, _task())
    await work.start_task(_session(), task, now=NOW)
    await work.complete_task(_session(), task, now=NOW + timedelta(minutes=50))
    assert world.sessions[0].end_reason == "completed" and task.worked_minutes == 50
    assert world.closed == [task.id]


@pytest.mark.asyncio
async def test_logged_work_counts_and_can_complete(world):
    task = _add(world, _task())
    await work.log_work(
        _session(), task, started_at=NOW - timedelta(hours=2), ended_at=NOW - timedelta(hours=1),
        complete=True, now=NOW,
    )
    [row] = world.sessions
    assert row.source == "retro" and row.end_reason == "completed"
    assert task.worked_minutes == 60 and world.closed == [task.id]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (NOW, NOW - timedelta(minutes=5), "after the start"),
        (NOW, NOW + timedelta(hours=1), "future"),
        (NOW - timedelta(hours=20), NOW, "16 hours"),
    ],
)
def test_logged_spans_are_validated(start, end, message):
    with pytest.raises(ValueError, match=message):
        work.validate_span(start, end, NOW)


@pytest.mark.asyncio
async def test_an_abandoned_session_counts_nothing_and_replans(world):
    task = _add(world, _task(scheduled_date=NOW, booked_minutes=60))
    await work.start_task(_session(), task, now=NOW)

    await work.end_abandoned(_session(), task, at=NOW + timedelta(hours=2))

    assert world.sessions[0].end_reason == "abandoned" and task.worked_minutes == 0
    assert world.logs[-1]["kind"] == "abandoned"
    assert [task_id for task_id, _ in world.scheduled] == [task.id]


@pytest.mark.asyncio
async def test_a_closed_task_cant_be_started(world):
    task = _add(world, _task())
    world.status[task.id] = "closed"
    with pytest.raises(ValueError):
        await work.start_task(_session(), task, now=NOW)


@pytest.mark.asyncio
async def test_closing_or_dismissing_a_running_task_ends_its_session(world):
    task = _add(world, _task())
    await work.start_task(_session(), task, now=NOW)

    work.stop_running(_session(), task, reason="dismissed", at=NOW + timedelta(minutes=15))

    assert world.sessions[0].end_reason == "dismissed" and task.running_since is None
    assert task.worked_minutes == 15
    # Nothing left running for the next Start to "switch" away from.
    other = _add(world, _task(title="Next"))
    await work.start_task(_session(), other, now=NOW + timedelta(hours=3))
    assert world.sessions[0].ended_at == NOW + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_spontaneous_work_is_marked_so_it_never_trains_the_estimates(world, monkeypatch):
    created = []

    def fake_create(_s, payload):
        created.append(payload)
        return _add(world, _task(title=payload.title, estimation=payload.estimation))

    monkeypatch.setattr(work.tasks_store, "create", fake_create)
    monkeypatch.setattr(work, "publish_input", lambda *_a: None)
    session = SimpleNamespace(commit=lambda: None, add=lambda _row: None)

    await work.log_spontaneous(
        session, title="Fixed the bike", started_at=NOW - timedelta(minutes=40), ended_at=NOW, now=NOW,
    )

    [payload] = created
    assert payload.estimation == 40 and payload.estimation_source == work.LOGGED
    assert world.closed == [world.sessions[0].task_id]
