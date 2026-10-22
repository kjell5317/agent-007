"""The "Reschedule" tap, with the planner and the schedule log faked."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.task import reschedule as reschedule_svc

START = datetime(2026, 10, 28, 22, 15, tzinfo=timezone.utc)


@pytest.fixture
def planner(monkeypatch):
    state = SimpleNamespace(calls=[], logged=[])

    async def schedule_task(_session, task, **kw):
        state.calls.append(kw)
        return None

    async def snapshot(_session, task, start, end, **_kw):
        return {"slot": {"h_start:15": 1.0}}

    monkeypatch.setattr(reschedule_svc, "schedule_task", schedule_task)
    monkeypatch.setattr(reschedule_svc, "features_snapshot", snapshot)
    monkeypatch.setattr(reschedule_svc.log_store, "add", lambda _s, **kw: state.logged.append(kw))
    monkeypatch.setattr("app.services.learn.jobs.refit_soon", lambda: None)
    return state


def make_task(**kw):
    fields = dict(
        id=uuid.uuid4(), scheduled_date=START, estimation=60, booked_minutes=None,
        calendar_event_id="ev", schedule_state="placed",
    )
    return SimpleNamespace(**{**fields, **kw})


def session():
    return SimpleNamespace(commit=lambda: None)


@pytest.mark.asyncio
async def test_the_rejected_slot_is_logged_then_blocked(planner):
    task = make_task()
    await reschedule_svc.reschedule_task(session(), task)
    [row] = planner.logged
    assert (row["kind"], row["actor"]) == ("rescheduled", "user")
    assert row["slot_start"] == START and row["slot_end"] == START + timedelta(minutes=60)
    assert row["features"]["slot"]
    [call] = planner.calls
    assert call["block"].start == START and not call.get("pull_in")


@pytest.mark.asyncio
async def test_a_later_task_is_pulled_in(planner):
    task = make_task(schedule_state="deferred", scheduled_date=None, calendar_event_id=None)
    await reschedule_svc.reschedule_task(session(), task)
    assert planner.logged == []
    assert planner.calls == [{"pull_in": True}]
