"""Start cues, missed slots, running sessions, and the replan helpers."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.api import notifications
from app.services.notifications import messages
from app.services.plan import cue
from app.services.plan import features as F
from app.services.plan import replan
from app.services.plan import solver as S
from app.services.plan.timeline import Grid, Item, TaskAttrs

TZ = ZoneInfo("Europe/Berlin")
START = datetime(2026, 10, 20, 14, tzinfo=TZ)
END = START + timedelta(hours=1)


def test_running_extension_steps_from_the_session_start_and_caps():
    # A session that started 17 minutes after the booked end.
    since = END + timedelta(minutes=17)
    assert cue.running_end(START, END, since, since) == since + timedelta(minutes=15)
    assert cue.running_end(START, END, since + timedelta(minutes=14), since) == since + timedelta(minutes=15)
    assert cue.running_end(START, END, since + timedelta(minutes=15), since) == since + timedelta(minutes=30)
    # Never short of the booked end, never past 2× the booked duration.
    early = END - timedelta(minutes=30)
    assert cue.running_end(START, END, early, early) == END
    # A 1-hour slot is forgotten 90 minutes past its end, not at 2× already.
    assert cue.running_end(START, END, END + timedelta(hours=5), since) == END + timedelta(minutes=90)
    assert not cue.abandoned(START, END, END + timedelta(minutes=89))
    assert cue.abandoned(START, END, END + timedelta(minutes=90))
    # A resumed task with 10 minutes left isn't dropped after 20.
    short_end = START + timedelta(minutes=10)
    assert not cue.abandoned(START, short_end, START + timedelta(minutes=20))
    # A long one gets twice its length.
    long_end = START + timedelta(hours=3)
    assert cue.abandon_point(START, long_end) == START + timedelta(hours=6)


def test_cue_window_and_missed_slot():
    slot = (START, END)
    assert not cue.cue_due(slot, START - timedelta(minutes=1))
    assert cue.cue_due(slot, START)
    assert cue.cue_due(slot, START + timedelta(minutes=9))
    assert not cue.cue_due(slot, START + timedelta(minutes=10))
    # A 5-minute slot stops cueing when it ends.
    assert not cue.cue_due((START, START + timedelta(minutes=5)), START + timedelta(minutes=6))
    assert not cue.missed(slot, END + timedelta(minutes=14))
    assert cue.missed(slot, END + timedelta(minutes=15))


def _task(**kw):
    base = dict(
        id=uuid.uuid4(), title="Write report", scheduled_date=START, running_since=None,
        estimation=60, booked_minutes=None, worked_minutes=0, is_container=False,
        calendar_event_id="ev-1", schedule_state="placed", deadline_kind="hard",
        due_date=START + timedelta(days=1),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _session():
    return SimpleNamespace(commit=lambda: None)


@pytest.mark.asyncio
async def test_cue_goes_out_once_per_slot_with_the_slot_snapshot(monkeypatch):
    task = _task()
    already: set[str] = set()
    sent, logged = [], []

    async def send_once(_session, *, key, kind, message, task_id=None, fingerprint=""):
        if key in already:
            return False
        already.add(key)
        sent.append(message)
        return True

    async def snapshot(_session, _task, start, end, *, now):
        return {"slot": {"h_start:14": 1.0}}

    monkeypatch.setattr(cue.tasks_store, "open_scheduled_between", lambda _s, **_kw: [task])
    monkeypatch.setattr("app.services.notifications.notify.send_once", send_once)
    monkeypatch.setattr("app.services.plan.snapshot.features_snapshot", snapshot)
    monkeypatch.setattr(cue.log_store, "add", lambda _s, **kw: logged.append(kw))

    assert await cue._send_cues(_session(), START + timedelta(minutes=1)) == 1
    assert await cue._send_cues(_session(), START + timedelta(minutes=2)) == 0
    [message] = sent
    assert message.tag == messages.task_tag(task.id, "cue")
    assert [a for a, _ in message.actions] == [
        messages.ACTION_START_TASK, messages.ACTION_SNOOZE_START, messages.ACTION_NOT_TODAY,
    ]
    assert [(row["kind"], row["slot_end"]) for row in logged] == [("cue_sent", END)]
    assert logged[0]["features"]["slot"]


@pytest.mark.asyncio
async def test_no_cue_for_a_running_or_unmirrored_task(monkeypatch):
    tasks = [_task(running_since=START), _task(calendar_event_id=None)]
    monkeypatch.setattr(cue.tasks_store, "open_scheduled_between", lambda _s, **_kw: tasks)

    async def fail(*_a, **_kw):
        raise AssertionError("no cue expected")

    monkeypatch.setattr("app.services.notifications.notify.send_once", fail)
    assert await cue._send_cues(_session(), START + timedelta(minutes=1)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "kind", "until"),
    [
        (messages.ACTION_SNOOZE_START, "cue_snoozed", START + timedelta(minutes=35)),
        (messages.ACTION_NOT_TODAY, "cue_declined", datetime(2026, 10, 21, tzinfo=TZ)),
    ],
)
async def test_cue_answers_move_the_task_off_the_slot(monkeypatch, action, kind, until):
    task = _task()
    logged, moved, cleared = [], [], []

    async def fake_clear(_session, task_id, *, kinds=None):
        cleared.append(kinds)

    async def fake_schedule(_session, received, *, block):
        moved.append((block.start, block.end))
        return None

    monkeypatch.setattr(cue.log_store, "add", lambda _s, **kw: logged.append(kw))
    monkeypatch.setattr("app.services.notifications.notify.clear_task_notifications", fake_clear)
    monkeypatch.setattr("app.services.plan.schedule.schedule_task", fake_schedule)
    monkeypatch.setattr("app.services.learn.jobs.refit_soon", lambda: None)
    monkeypatch.setattr(
        "app.db.clients.notification_ledger.get",
        lambda _s, key: SimpleNamespace(key=key, cleared_at=None),
    )

    await cue.handle_cue_action(_session(), task, action, now=START + timedelta(minutes=5))

    assert [row["kind"] for row in logged] == [kind]
    assert logged[0]["slot_start"] == START and logged[0]["slot_end"] == END
    assert moved == [(START, until)]
    assert cleared == [{"start_cue"}]


@pytest.mark.asyncio
async def test_a_tap_on_a_cue_whose_slot_moved_on_counts_for_nothing(monkeypatch):
    # The cue asked about 14:00; the task has since moved to tomorrow.
    task = _task(scheduled_date=START + timedelta(days=1))
    logged, moved, cleared = [], [], []

    async def fake_clear(_session, task_id, *, kinds=None):
        cleared.append(kinds)

    async def fake_schedule(*_a, **_kw):
        moved.append(True)

    monkeypatch.setattr(cue.log_store, "add", lambda _s, **kw: logged.append(kw))
    monkeypatch.setattr("app.services.notifications.notify.clear_task_notifications", fake_clear)
    monkeypatch.setattr("app.services.plan.schedule.schedule_task", fake_schedule)
    # No cue was ever sent for tomorrow's slot.
    monkeypatch.setattr("app.db.clients.notification_ledger.get", lambda _s, key: None)

    await cue.handle_cue_action(_session(), task, messages.ACTION_NOT_TODAY, now=START + timedelta(minutes=5))

    assert logged == [] and moved == [] and cleared == [{"start_cue"}]


@pytest.mark.asyncio
async def test_stale_cues_are_cleared(monkeypatch):
    now = START + timedelta(minutes=5)
    current = _task()
    moved = _task(scheduled_date=START + timedelta(days=1))
    started = _task(running_since=START)
    rows = [
        SimpleNamespace(key=cue.cue_key(t.id, START), tag=f"tag-{n}", task_id=t.id, cleared_at=None)
        for n, t in enumerate((current, moved, started))
    ]
    by_id = {t.id: t for t in (current, moved, started)}
    cleared = []

    async def fake_clear(tag):
        cleared.append(tag)

    monkeypatch.setattr("app.db.clients.notification_ledger.showing_of_kind", lambda _s, _k: rows)
    monkeypatch.setattr("app.db.clients.notification_ledger.record_cleared", lambda *_a: None)
    monkeypatch.setattr("app.services.notifications.push.clear", fake_clear)
    monkeypatch.setattr(cue.tasks_store, "get", lambda _s, task_id: by_id[task_id])

    assert await cue._clear_stale_cues(_session(), now) == 2
    assert cleared == ["tag-1", "tag-2"]


@pytest.mark.asyncio
async def test_missed_slots_trigger_one_solve(monkeypatch):
    now = END + timedelta(minutes=20)
    tasks = [
        _task(),  # ended 20 min ago, unstarted
        _task(running_since=START),  # running: its slot stays
        _task(scheduled_date=now - timedelta(minutes=30)),  # still under way
    ]
    monkeypatch.setattr(cue.tasks_store, "overdue_scheduled_open", lambda _s, **_kw: tasks)
    solves = []

    async def fake_replan(_session, *, reason):
        solves.append(reason)

    monkeypatch.setattr("app.services.plan.replan.request_replan", fake_replan)
    assert await cue._replan_missed(_session(), now) == 1
    assert solves == ["missed_slot"]


def test_only_a_recently_missed_hard_deadline_costs_points(monkeypatch):
    now = START
    tasks = [
        _task(due_date=now - timedelta(hours=2)),  # hard, just missed
        _task(due_date=now - timedelta(days=3)),  # hard, missed long ago
        _task(due_date=now - timedelta(hours=2), deadline_kind="soft"),
        _task(due_date=now + timedelta(hours=2)),  # still ahead
    ]
    monkeypatch.setattr(cue.tasks_store, "open_schedulable", lambda _s: tasks)
    penalized = []
    monkeypatch.setattr(
        "app.services.points.subtract_missed_deadline_penalty",
        lambda _s, task: penalized.append(task.id) or True,
    )
    monkeypatch.setattr("app.events.publish_points", lambda _s: None)
    assert cue._penalize_missed_deadlines(_session(), now) == 1
    assert penalized == [tasks[0].id]


@pytest.mark.asyncio
async def test_closing_a_running_task_drops_its_event(monkeypatch):
    from app.services.task import close

    deleted = []

    async def fake_delete(_session, task):
        deleted.append(task.id)

    async def no_op(*_args, **_kwargs):
        return None

    task = _task(running_since=END, kotx_task_id=None)
    anchor = SimpleNamespace(id=uuid.uuid4(), status="open", processed_at=None)
    monkeypatch.setattr(close.tasks_store, "get", lambda _s, _id: task)
    monkeypatch.setattr(close.raw_inputs_store, "latest_for_task", lambda _s, _id: anchor)
    monkeypatch.setattr(close, "vacated_commute_window", lambda _t: None)
    monkeypatch.setattr(close, "_log_close", no_op)
    monkeypatch.setattr(close, "award_for_task", lambda *_a, **_kw: False)
    monkeypatch.setattr(close, "publish_task", lambda *_a: None)
    monkeypatch.setattr(close, "publish_input", lambda *_a: None)
    monkeypatch.setattr(close, "delete_task_event", fake_delete)
    monkeypatch.setattr(close, "clear_task_notifications", no_op)
    monkeypatch.setattr(close, "replan_vacated_window", no_op)
    monkeypatch.setattr("app.services.task.work.stop_running", lambda *_a, **_kw: None)
    await close.close_task(_session(), task.id)
    assert deleted == [task.id]
    assert task.running_since is None


@pytest.mark.asyncio
async def test_extension_moves_the_next_task_even_once_it_began(monkeypatch):
    # The session outlasts its slot, so the task booked right after has
    # already begun by the time the extension reaches it.
    now = datetime.now(TZ)
    booked_end = now - timedelta(minutes=20)
    extension_end = booked_end + timedelta(minutes=30)

    def slot_task(start, minutes):
        return SimpleNamespace(id=uuid.uuid4(), scheduled_date=start, running_since=None,
                               estimation=minutes, booked_minutes=None)

    running = slot_task(booked_end - timedelta(hours=1), 60)
    begun = slot_task(booked_end, 30)
    later = slot_task(extension_end, 30)
    monkeypatch.setattr(cue.tasks_store, "open_scheduled_between", lambda _s, **_kw: [begun, later])
    moved = []

    async def fake_schedule(_session, task, *, block):
        moved.append((task.id, block.start, block.end))

    monkeypatch.setattr("app.services.plan.schedule.schedule_task", fake_schedule)
    await cue._clear_way(_session(), running, booked_end, extension_end)
    assert moved == [(begun.id, booked_end, extension_end)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", [messages.ACTION_START_TASK, messages.ACTION_SNOOZE_START, messages.ACTION_NOT_TODAY],
)
async def test_notification_router_dispatches_cue_answers(monkeypatch, action):
    task = _task()
    handled = []

    async def fake_start(_session, received):
        handled.append((received.id, "start"))

    async def fake_cue(_session, received, received_action):
        handled.append((received.id, received_action))

    monkeypatch.setattr(notifications.tasks_store, "get", lambda _s, _id: task)
    monkeypatch.setattr(notifications, "start_task", fake_start)
    monkeypatch.setattr(notifications, "handle_cue_action", fake_cue)
    monkeypatch.setattr(notifications, "get_settings", lambda: SimpleNamespace(home_assistant_action_secret=""))
    result = await notifications.handle_action(
        notifications.ActionPayload(action=action, tag=messages.task_tag(task.id, "cue")),
        SimpleNamespace(headers={}, query_params={}),
        session=SimpleNamespace(),
    )
    expected = "start" if action == messages.ACTION_START_TASK else action
    assert result["ok"] and handled == [(task.id, expected)]


# --- replan helpers -------------------------------------------------------------


def _attrs(**kw) -> TaskAttrs:
    base = dict(task_id=uuid.uuid4(), due=datetime(2026, 12, 1, 18, tzinfo=TZ), deadline_kind="hard",
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
