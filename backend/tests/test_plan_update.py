"""`update_task_to_calendar` with the calendar faked."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.plan import update as plan_update

START = datetime(2026, 10, 26, 19, 15, tzinfo=timezone.utc)


@pytest.fixture
def calendar(monkeypatch):
    state = SimpleNamespace(patches=[], solves=[])

    async def update_task_event(_session, task, *, start=None, end=None, changed_fields=None):
        state.patches.append((start, end, changed_fields))

    async def schedule_task(_session, task, **_kw):
        state.solves.append(task.id)

    monkeypatch.setattr("app.services.calendar.update_task_event", update_task_event)
    monkeypatch.setattr("app.services.plan.schedule.schedule_task", schedule_task)
    return state


def make_task(**kw):
    return SimpleNamespace(
        id=uuid.uuid4(), calendar_event_id="ev", scheduled_date=START, estimation=60,
        booked_minutes=None, location=None, **kw,
    )


@pytest.mark.asyncio
async def test_duration_edit_resizes_the_event_before_the_solve(calendar):
    task = make_task()
    await plan_update.update_task_to_calendar(object(), task, changed_fields={"estimation"})
    [(start, end, _)] = calendar.patches
    assert start == START and end - start == timedelta(minutes=60)
    assert calendar.solves == [task.id]


@pytest.mark.asyncio
async def test_title_edit_keeps_the_event_times(calendar):
    await plan_update.update_task_to_calendar(object(), make_task(), changed_fields={"title"})
    assert [(start, end) for start, end, _ in calendar.patches] == [(None, None)]


@pytest.mark.asyncio
async def test_duration_edit_without_an_event_only_solves(calendar):
    task = make_task()
    task.calendar_event_id = None
    task.scheduled_date = None
    await plan_update.update_task_to_calendar(object(), task, changed_fields={"estimation"})
    assert calendar.patches == [] and calendar.solves == [task.id]
