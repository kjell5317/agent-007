"""End-of-slot check-ins, running extensions, and the replan helpers."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.api import notifications
from app.services.plan import checkin
from app.services.plan import features as F
from app.services.plan import replan
from app.services.plan import solver as S
from app.services.plan.timeline import Grid, Item, TaskAttrs

TZ = ZoneInfo("Europe/Berlin")
START = datetime(2026, 10, 20, 14, tzinfo=TZ)
END = START + timedelta(hours=1)


def test_running_extension_steps_from_the_answer_and_caps():
    # "Still working" pressed 17 minutes after the booked end.
    since = END + timedelta(minutes=17)
    assert checkin.running_end(START, END, since, since) == since + timedelta(minutes=15)
    assert checkin.running_end(START, END, since + timedelta(minutes=14), since) == since + timedelta(minutes=15)
    assert checkin.running_end(START, END, since + timedelta(minutes=15), since) == since + timedelta(minutes=30)
    # Never short of the booked end, never past 2× the booked duration.
    early = END - timedelta(minutes=30)
    assert checkin.running_end(START, END, early, early) == END
    assert checkin.running_end(START, END, END + timedelta(hours=5), since) == START + timedelta(hours=2)
    assert not checkin.abandoned(START, END, START + timedelta(minutes=119))
    assert checkin.abandoned(START, END, START + timedelta(hours=2))


def test_awaiting_checkin_window():
    assert checkin.awaiting_checkin(END, END + timedelta(minutes=5), resolved=False)
    assert checkin.awaiting_checkin(END, END + timedelta(hours=2), resolved=False)
    assert not checkin.awaiting_checkin(END, END + timedelta(hours=2, minutes=16), resolved=False)
    assert not checkin.awaiting_checkin(END, END + timedelta(minutes=5), resolved=True)


@pytest.fixture
def logged(monkeypatch):
    rows: list[dict] = []
    sent = SimpleNamespace(
        kind="checkin_sent", slot_start=START, slot_end=END, features={"slot": {"h_start:14": 1.0}},
    )
    monkeypatch.setattr(checkin.log_store, "add", lambda _s, **kw: rows.append(kw))
    monkeypatch.setattr(checkin.log_store, "latest_of_kind", lambda _s, _t, _k: sent)
    monkeypatch.setattr(
        checkin.log_store, "for_slot", lambda _s, _t, _start: [sent] + [SimpleNamespace(**r) for r in rows]
    )
    monkeypatch.setattr("app.services.learn.jobs.refit_soon", lambda: None)
    return rows


def _task():
    return SimpleNamespace(id=uuid.uuid4(), title="Write report", scheduled_date=START,
                           running_since=None, estimation=60, booked_minutes=None)


def _session():
    return SimpleNamespace(commit=lambda: None)


@pytest.mark.asyncio
async def test_done_confirms_the_asked_slot_and_closes(monkeypatch, logged):
    closed = []

    async def fake_close(_session, task_id):
        closed.append(task_id)

    monkeypatch.setattr("app.services.task.close.close_task", fake_close)
    task = _task()
    await checkin.handle_checkin_action(_session(), task, checkin.ACTION_CHECKIN_DONE)
    assert [r["kind"] for r in logged] == ["done_confirmed"]
    assert logged[0]["slot_end"] == END and logged[0]["features"]["slot"]
    assert closed == [task.id]


@pytest.mark.asyncio
async def test_still_working_extends_the_event_right_away(monkeypatch, logged):
    cleared, written = [], []

    async def fake_clear(task_id):
        cleared.append(task_id)

    async def no_events(_session, _start, _end):
        return []

    async def fake_write(_session, task, *, start, end):
        written.append((start, end))

    async def no_clearing(*_args):
        return None

    monkeypatch.setattr("app.services.notify.clear_task_notification", fake_clear)
    monkeypatch.setattr("app.events.publish_task", lambda _s, _t: None)
    monkeypatch.setattr("app.services.plan.snapshot.calendar_items", no_events)
    monkeypatch.setattr("app.services.calendar.update_task_event", fake_write)
    monkeypatch.setattr(checkin, "_clear_way", no_clearing)
    task = _task()
    # Answered 17 minutes after a slot that ended earlier.
    task.scheduled_date = datetime.now(TZ) - timedelta(minutes=77)
    await checkin.handle_checkin_action(_session(), task, checkin.ACTION_CHECKIN_STILL_WORKING)
    assert [r["kind"] for r in logged] == ["still_working"]
    assert cleared == [task.id]
    [(start, end)] = written
    assert start == task.scheduled_date
    assert end == task.running_since + checkin.EXTENSION_STEP


@pytest.mark.asyncio
async def test_not_done_deducts_and_replans(monkeypatch, logged):
    penalties, replanned = [], []

    def fake_penalty(_session, task, *, scheduled_date):
        penalties.append(scheduled_date)
        return True

    async def fake_schedule(_session, task, **kw):
        replanned.append(kw)
        return None

    async def fake_clear(task_id):
        pass

    monkeypatch.setattr("app.services.points.subtract_scheduled_overdue_penalty", fake_penalty)
    monkeypatch.setattr("app.events.publish_points", lambda _s: None)
    monkeypatch.setattr("app.services.notify.clear_task_notification", fake_clear)
    monkeypatch.setattr("app.services.plan.schedule.schedule_task", fake_schedule)
    task = _task()
    await checkin.handle_checkin_action(_session(), task, checkin.ACTION_CHECKIN_NOT_DONE)
    assert [r["kind"] for r in logged] == ["not_done"]
    assert penalties == [START]
    assert replanned == [{"notify": False}]


@pytest.mark.asyncio
async def test_extension_moves_the_next_task_even_once_it_began(monkeypatch):
    # "Still working" lands ≥15 min after the booked end, so the task booked
    # right after has already begun by the time the extension reaches it.
    now = datetime.now(TZ)
    booked_end = now - timedelta(minutes=20)
    extension_end = booked_end + timedelta(minutes=30)

    def slot_task(start, minutes):
        return SimpleNamespace(id=uuid.uuid4(), scheduled_date=start, running_since=None,
                               estimation=minutes, booked_minutes=None)

    running = slot_task(booked_end - timedelta(hours=1), 60)
    begun = slot_task(booked_end, 30)
    later = slot_task(extension_end, 30)
    monkeypatch.setattr(checkin.tasks_store, "open_scheduled_between", lambda _s, **_kw: [begun, later])
    moved = []

    async def fake_schedule(_session, task, *, block, notify):
        moved.append((task.id, block.start, block.end))

    monkeypatch.setattr("app.services.plan.schedule.schedule_task", fake_schedule)
    await checkin._clear_way(_session(), running, booked_end, extension_end)
    assert moved == [(begun.id, booked_end, extension_end)]


@pytest.mark.asyncio
async def test_late_answer_to_a_settled_checkin_only_closes(monkeypatch, logged):
    monkeypatch.setattr(
        checkin.log_store, "for_slot",
        lambda _s, _t, _start: [SimpleNamespace(kind="checkin_sent"), SimpleNamespace(kind="no_response")],
    )
    closed = []

    async def fake_close(_session, task_id):
        closed.append(task_id)

    monkeypatch.setattr("app.services.task.close.close_task", fake_close)
    task = _task()
    await checkin.handle_checkin_action(_session(), task, checkin.ACTION_CHECKIN_DONE)
    assert logged == [] and closed == [task.id]


@pytest.mark.asyncio
async def test_notification_router_dispatches_checkins(monkeypatch):
    task = _task()
    handled = []

    async def fake_handle(_session, received, action):
        handled.append((received.id, action))

    monkeypatch.setattr(notifications.tasks_store, "get", lambda _s, _id: task)
    monkeypatch.setattr(notifications, "handle_checkin_action", fake_handle)
    monkeypatch.setattr(notifications, "get_settings", lambda: SimpleNamespace(home_assistant_action_secret=""))
    result = await notifications.handle_action(
        notifications.ActionPayload(action=checkin.ACTION_CHECKIN_NOT_DONE, tag=f"task-{task.id}"),
        SimpleNamespace(headers={}, query_params={}),
        session=SimpleNamespace(),
    )
    assert result["ok"] and handled == [(task.id, checkin.ACTION_CHECKIN_NOT_DONE)]


# --- replan helpers -------------------------------------------------------------


def _attrs(**kw) -> TaskAttrs:
    base = dict(task_id=uuid.uuid4(), due=datetime(2026, 12, 1, 18, tzinfo=TZ), deadline_kind="firm",
                label=None, work_type=None, effort=None)
    base.update(kw)
    return TaskAttrs(**base)


def test_deferral_gets_riskier_as_free_time_shrinks():
    grid = Grid(date(2026, 10, 19), 7, TZ)
    kw = dict(grid=grid, weights=F.prior_mean(), rho=0.5)
    roomy = replan._defer_utility(_attrs(), 60, free_until_due=10_000, **kw)
    tight = replan._defer_utility(_attrs(), 60, free_until_due=100, **kw)
    assert roomy > tight
    # A good slot today beats waiting: earlier days are preferred.
    assert roomy < 0


def test_capacity_bounds_grow_week_by_week():
    grid = Grid(date(2026, 10, 19), 7, TZ)
    first = date(2026, 10, 26)
    free = {first + timedelta(days=d): 300.0 for d in range(60)}
    bounds = replan._capacity_bounds(grid, free, 0.5, [grid.horizon + 20 * S.SLOTS_PER_DAY])
    assert [b.capacity_min for b in bounds] == [0.5 * 300 * 7 * n for n in range(1, len(bounds) + 1)]
    assert bounds[-1].due_by >= grid.horizon + 20 * S.SLOTS_PER_DAY


def test_warm_start_keeps_valid_slots_and_places_the_rest():
    grid = Grid(date(2026, 10, 19), 2, TZ)
    event = Item(datetime(2026, 10, 19, 13, tzinfo=TZ), datetime(2026, 10, 19, 15, tzinfo=TZ), "event")

    def movable(key, current=None, schedule_state="placed"):
        task = SimpleNamespace(schedule_state=schedule_state)
        st = S.SolverTask(key=key, dur=12, earliest=9 * 12, due=2 * 288 - 1, current=current, stability=1.0)
        return replan._Movable(task, _attrs(), 60, None, st, 60)

    keep = movable("keep", current=11 * 12)
    clash = movable("clash", current=13 * 12 + 6)  # overlaps the event now
    new = movable("new", schedule_state="unplaced")
    replan._warm_start(grid, [keep, clash, new], [event], F.prior_mean())
    assert keep.solver_task.hint == 11 * 12
    for m in (clash, new):
        hint = m.solver_task.hint
        assert hint is not None and 10 * 12 <= hint % 288 <= 18 * 12
        assert hint + 12 <= 13 * 12 or hint >= 15 * 12
    assert clash.solver_task.hint != new.solver_task.hint
