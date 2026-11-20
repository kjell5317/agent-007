"""A new task planned now gets no push of its own: its calendar popup is
going off already."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.services.notifications import notify  # noqa: E402

NOW = datetime(2026, 11, 20, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def lead(monkeypatch):
    monkeypatch.setattr(notify, "get_settings", lambda: SimpleNamespace(reminder_lead_minutes=15))


def _task(starts_in: timedelta | None, *, event: str | None = "ev-1"):
    return SimpleNamespace(
        id=uuid.uuid4(), title="Call the bank",
        scheduled_date=NOW + starts_in if starts_in is not None else None,
        calendar_event_id=event,
    )


@pytest.mark.parametrize(("starts_in", "announced"), [
    (timedelta(minutes=-5), True),
    (timedelta(minutes=15), True),
    # The next free quarter hour past the lead.
    (timedelta(minutes=30), True),
    (timedelta(minutes=31), False),
    (timedelta(hours=3), False),
])
def test_only_a_slot_whose_popup_is_due_counts_as_planned_now(starts_in, announced):
    assert notify.calendar_announces(_task(starts_in), NOW) is announced


def test_without_a_slot_or_an_event_nothing_announces_it():
    assert notify.calendar_announces(_task(None), NOW) is False
    assert notify.calendar_announces(_task(timedelta(minutes=15), event=None), NOW) is False


@pytest.mark.asyncio
async def test_a_task_planned_now_gets_no_push(monkeypatch):
    sent = []

    async def send_once(_session, **kw):
        sent.append(kw["key"])
        return True

    monkeypatch.setattr(notify, "send_once", send_once)
    monkeypatch.setattr(notify, "datetime", SimpleNamespace(now=lambda _tz: NOW))
    monkeypatch.setattr(notify.messages, "new_task", lambda task: task.title)

    now_task, later_task = _task(timedelta(minutes=20)), _task(timedelta(hours=2))
    await notify.notify_new_task(SimpleNamespace(), now_task)
    await notify.notify_new_task(SimpleNamespace(), later_task)

    assert sent == [f"created:{later_task.id}"]
