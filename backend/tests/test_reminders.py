from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.calendar import SILENT_REMINDERS, popup_reminders, reminders_differ  # noqa: E402
from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.commute.legs import HOME, PlannedLeg  # noqa: E402
from app.services.commute.planner import (  # noqa: E402
    _REMINDERS_MANAGED_PROP,
    _desired_anchor_reminders,
    _reminders_for_leg,
)

SETTINGS = SimpleNamespace(reminder_lead_minutes=15)

TASK_PROPS = {"managed_by": "plan_service", "kind": "task", "task_id": "x"}


def _event(props=None, reminders=None):
    when = datetime(2026, 7, 10, 14, tzinfo=timezone.utc)
    return CalendarEvent(
        id="ev1",
        calendar_id="primary",
        summary="Event",
        description=None,
        start=when,
        end=when,
        all_day=False,
        location="Somewhere 1",
        html_link=None,
        private_properties=props or {},
        raw={"reminders": reminders} if reminders is not None else {},
    )


def _leg(dest_anchor):
    when = datetime(2026, 7, 10, 13, tzinfo=timezone.utc)
    return PlannedLeg(
        origin_anchor="a", dest_anchor=dest_anchor, origin="A", destination="B",
        mode="bicycling", depart=when, arrive=when,
    )


def test_arriving_leg_carries_popup_home_leg_is_silent(monkeypatch):
    import app.services.commute.planner as planner

    monkeypatch.setattr(planner, "get_settings", lambda: SETTINGS)
    assert _reminders_for_leg(_leg("ev1")) == popup_reminders(15)
    assert _reminders_for_leg(_leg(HOME)) == SILENT_REMINDERS


def test_task_event_reminders_follow_arriving_leg():
    task_event = _event(props=TASK_PROPS)
    assert _desired_anchor_reminders(task_event, True, SETTINGS) == (SILENT_REMINDERS, None)
    assert _desired_anchor_reminders(task_event, False, SETTINGS) == (popup_reminders(15), None)


def test_foreign_event_is_claimed_and_restored():
    plain = _event()
    desired, props = _desired_anchor_reminders(plain, True, SETTINGS)
    assert desired == SILENT_REMINDERS
    assert props == {_REMINDERS_MANAGED_PROP: "true"}

    claimed = _event(props={_REMINDERS_MANAGED_PROP: "true"})
    desired, props = _desired_anchor_reminders(claimed, False, SETTINGS)
    assert desired == {"useDefault": True}
    assert props == {_REMINDERS_MANAGED_PROP: ""}

    # Foreign event with no leg that we never touched → hands off.
    assert _desired_anchor_reminders(plain, False, SETTINGS) == (None, None)


def test_reminders_differ_normalizes():
    ev_default = _event()  # no reminders field → useDefault
    assert reminders_differ(ev_default, popup_reminders(15))
    assert not reminders_differ(ev_default, {"useDefault": True})

    ev_popup = _event(reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]})
    assert not reminders_differ(ev_popup, popup_reminders(15))
    assert reminders_differ(ev_popup, SILENT_REMINDERS)
    assert reminders_differ(ev_popup, popup_reminders(30))
