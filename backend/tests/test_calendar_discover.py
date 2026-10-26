from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.calendar.events import is_declined_event  # noqa: E402
from app.timezones import user_tz  # noqa: E402

GYM = "Gymstreet 5, Munich"
OFFICE = "Officeplatz 2, Munich"
LIBRARY = "Bookweg 3, Munich"
EVENT_BUFFER = timedelta(minutes=15)


def _day_at(hour: int, minute: int = 0, day_offset: int = 3) -> datetime:
    base = datetime.now(user_tz()) + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _calendar_event(
    event_id,
    start,
    end,
    *,
    location=None,
    all_day=False,
    props=None,
    transparency=None,
    raw=None,
):
    raw_payload = dict(raw or {})
    if transparency:
        raw_payload["transparency"] = transparency
    return CalendarEvent(
        id=event_id,
        calendar_id="primary",
        summary="Event",
        description=None,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        html_link=None,
        private_properties=props or {},
        raw=raw_payload,
    )


def test_declined_event_predicate_uses_self_attendee_status():
    def event_with(attendees):
        return _calendar_event(
            "event",
            _day_at(9),
            _day_at(10),
            raw={"attendees": attendees} if attendees is not None else {},
        )

    assert not is_declined_event(event_with(None))
    assert not is_declined_event(
        event_with([{"self": False, "responseStatus": "declined"}])
    )
    assert not is_declined_event(
        event_with([
            {"self": True, "responseStatus": "accepted"},
            {"self": False, "responseStatus": "declined"},
        ])
    )
    assert not is_declined_event(event_with([{"self": True, "responseStatus": "tentative"}]))
    assert not is_declined_event(event_with([{"self": True, "responseStatus": "needsAction"}]))
    assert is_declined_event(event_with([{"self": True, "responseStatus": "declined"}]))


def test_location_blocker_is_a_timed_free_event_somewhere_physical():
    from app.services.calendar.events import is_location_blocker

    def event(**kw):
        return _calendar_event("ev", _day_at(12), _day_at(16), **kw)

    assert is_location_blocker(event(location=LIBRARY, transparency="transparent"))
    assert not is_location_blocker(event(location=LIBRARY))
    assert not is_location_blocker(event(transparency="transparent"))
    assert not is_location_blocker(
        event(location="https://zoom.us/j/123", transparency="transparent"),
    )
    assert not is_location_blocker(
        event(location=LIBRARY, transparency="transparent", all_day=True),
    )


def test_span_touches_window_drops_out_of_window_deletions():
    from app.services.calendar.discover import _span_touches_window

    window = (_day_at(8), _day_at(20))

    # A deletion inside the window drives a replan.
    assert _span_touches_window((_day_at(12), _day_at(13)), *window)
    # A zero-width span (recurring-instance tombstone) inside the window counts.
    assert _span_touches_window((_day_at(9), _day_at(9)), *window)
    # A far-future recurring instance (years past the look-ahead) is dropped.
    future = datetime(2031, 2, 6, 12, tzinfo=timezone.utc)
    assert not _span_touches_window((future, future), *window)
    # A garbage year-2000 span never touches the window either.
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert not _span_touches_window((past, past + timedelta(days=1)), *window)


def test_all_day_busy_event_triggers_overlap_with_task():
    from app.services.calendar.discover import _managed_conflicts

    task_props = {"managed_by": "plan_service", "kind": "task"}
    all_day = _calendar_event(
        "vacation", _day_at(0), _day_at(0, 0, day_offset=4), all_day=True,
    )
    task = _calendar_event("task-ev", _day_at(15), _day_at(16), props=task_props)

    assert _managed_conflicts(all_day, [task], EVENT_BUFFER) == [task]


def test_managed_conflicts_catches_event_within_gap_not_overlapping():
    from app.services.calendar.discover import _managed_conflicts

    task_props = {"managed_by": "plan_service", "kind": "task"}
    task = _calendar_event("task-ev", _day_at(15), _day_at(16), props=task_props)

    # Dragged to end 10 min before the task starts — no overlap, but inside
    # the 15-min event gap, so it must trigger a reschedule.
    close = _calendar_event("meeting", _day_at(14), _day_at(14, 50))
    assert _managed_conflicts(close, [task], EVENT_BUFFER) == [task]

    # A full gap away → left alone.
    clear = _calendar_event("meeting", _day_at(14), _day_at(14, 45))
    assert _managed_conflicts(clear, [task], EVENT_BUFFER) == []

    # A non-managed neighbour is never our concern.
    foreign = _calendar_event("foreign", _day_at(14), _day_at(14, 50))
    assert _managed_conflicts(close, [foreign], EVENT_BUFFER) == []


def test_blocked_leg_replans_the_tasks_it_serves():
    from app.services.calendar.discover import _reschedule_targets

    def leg(origin, dest):
        return _calendar_event(f"{origin}->{dest}", _day_at(12, 5), _day_at(12, 25), props={
            "managed_by": "plan_service", "kind": "commute",
            "origin_anchor": origin, "dest_anchor": dest,
        })

    task_ids = {"task-ev"}
    meeting = _calendar_event("meeting", _day_at(12, 10), _day_at(12, 20))
    task = _calendar_event(
        "task-ev", _day_at(11), _day_at(12), props={"managed_by": "plan_service", "kind": "task"},
    )

    # A newcomer on a task's trip moves the task; its legs follow.
    assert _reschedule_targets(meeting, leg("task-ev", "lecture"), task_ids) == ["task-ev"]
    assert _reschedule_targets(meeting, task, task_ids) == ["task-ev"]
    # A leg between fixed events has no task to move — re-derive it.
    assert _reschedule_targets(meeting, leg("home", "lecture"), task_ids) == ["home->lecture"]
    # The changed event's own leg just follows its anchor.
    lecture = _calendar_event("lecture", _day_at(12, 30), _day_at(13, 50))
    assert _reschedule_targets(lecture, leg("task-ev", "lecture"), task_ids) == ["task-ev->lecture"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end"),
    [
        # Flipped from show-as-free to busy right on top of the task.
        ((10, 30), (12, 0)),
        # Created inside the event gap after the task.
        ((11, 55), (11, 58)),
        # Created on the task's outbound leg.
        ((12, 10), (12, 20)),
    ],
)
async def test_discover_replans_task_when_read_event_blocks_it(monkeypatch, solves, start, end):
    from app.services.calendar import discover as discover_mod

    def raw(event_id, start, end, props=None):
        item = {
            "id": event_id, "status": "confirmed", "summary": event_id,
            "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
        }
        if props:
            item["extendedProperties"] = {"private": props}
        return item

    settings = SimpleNamespace(
        google_calendar_id="write", event_buffer_minutes=15, commute_enabled=False,
    )
    monkeypatch.setattr(discover_mod, "get_settings", lambda: settings)
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    token_row = SimpleNamespace(account_key="acct", extra={
        "calendar_sync_tokens": {"write": "w", "read": "r"},
        "calendar_sync_baselines": {"write": recent, "read": recent},
    })
    monkeypatch.setattr(
        discover_mod.oauth_tokens, "get_decrypted",
        lambda session, *, provider, account_key: token_row,
    )
    monkeypatch.setattr(discover_mod.oauth_tokens, "set_extra", lambda *a, **k: None)

    async def no_cache(session, *, active, cancelled):
        return {"upserted": 0, "embedded": 0}

    async def noop(*_args, **_kwargs):
        return None

    async def no_refresh(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(discover_mod, "cache_calendar_events", no_cache)
    monkeypatch.setattr(discover_mod, "refresh_recurring", no_refresh)
    monkeypatch.setattr(discover_mod, "_refresh_related_task_deadlines", noop)

    task = raw("task-ev", _day_at(11, 5), _day_at(11, 50), {
        "managed_by": "plan_service", "kind": "task", "task_id": "t1",
    })
    leg = raw("leg", _day_at(12, 5), _day_at(12, 25), {
        "managed_by": "plan_service", "kind": "commute",
        "origin_anchor": "task-ev", "dest_anchor": "lecture",
    })
    blocker = raw("seminar", _day_at(*start), _day_at(*end))

    class FakeClient:
        async def sync_events(self, cid, *, sync_token=None, time_min=None, time_max=None):
            return ([blocker] if cid == "read" else []), "next"

        async def list_events(self, cid, *, time_min=None, time_max=None):
            return [task, leg] if cid == "write" else [blocker]

    async def fake_authorized_client(session, account_key):
        return FakeClient()

    rescheduled = []

    async def fake_reschedule(session, event_id, *, account_key=None):
        rescheduled.append(event_id)

    monkeypatch.setattr(discover_mod, "authorized_client", fake_authorized_client)
    monkeypatch.setattr(discover_mod, "reschedule_event", fake_reschedule)

    await discover_mod.discover_updated_events(
        SimpleNamespace(commit=lambda: None), calendar_ids=["write", "read"],
    )

    assert rescheduled == ["task-ev"]


@pytest.fixture
def solves(monkeypatch):
    calls = []

    async def replan_commutes(_session, *, account_key=None):
        calls.append(account_key)

    monkeypatch.setattr("app.services.plan.commute.replan_commutes", replan_commutes)
    return calls


@pytest.mark.asyncio
async def test_any_foreign_timed_event_change_re_solves(solves):
    from app.services.calendar import discover as discover_mod

    commute_props = {"managed_by": "plan_service", "kind": "commute"}
    task_props = {"managed_by": "plan_service", "kind": "task"}
    declined = {"attendees": [{"self": True, "responseStatus": "declined"}]}
    gym = _calendar_event("gym", _day_at(14), _day_at(15), location=GYM)
    # An online / location-less event shapes legs too: they must not sit on it.
    call = _calendar_event("call", _day_at(11), _day_at(12), location="https://zoom.us/j/1")
    # Declining frees the slot the legs were routed around.
    skipped = _calendar_event("skipped", _day_at(12), _day_at(13), location=GYM, raw=declined)
    # Our own writes coming back through the sync, and all-day events, don't.
    leg = _calendar_event("leg", _day_at(13), _day_at(13, 30), location=GYM, props=commute_props)
    task = _calendar_event("task", _day_at(16), _day_at(17), props=task_props)
    holiday = _calendar_event("holiday", _day_at(0), _day_at(23), location=GYM, all_day=True)

    for changed in (gym, call, skipped):
        await discover_mod._plan_legs_for_changed_events(None, [changed], account_key=None)
    assert len(solves) == 3

    await discover_mod._plan_legs_for_changed_events(None, [leg, task, holiday], account_key=None)
    assert len(solves) == 3


@pytest.mark.asyncio
async def test_a_deletion_re_solves(solves):
    from app.services.calendar import discover as discover_mod

    await discover_mod._plan_legs_for_changed_events(None, [], deleted=True, account_key="acct")
    assert solves == ["acct"]


@pytest.mark.asyncio
async def test_sync_overflow_drops_token_to_rebaseline(monkeypatch):
    from app.services.calendar import discover as discover_mod

    settings = SimpleNamespace(
        google_calendar_id="primary", event_buffer_minutes=10, commute_enabled=False,
    )
    monkeypatch.setattr(discover_mod, "get_settings", lambda: settings)

    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    token_row = SimpleNamespace(
        account_key="acct",
        extra={
            "calendar_sync_tokens": {"primary": "stale-token"},
            "calendar_sync_baselines": {"primary": recent},
        },
    )
    monkeypatch.setattr(
        discover_mod.oauth_tokens, "get_decrypted",
        lambda session, *, provider, account_key: token_row,
    )

    captured = {}
    monkeypatch.setattr(
        discover_mod.oauth_tokens, "set_extra",
        lambda session, *, provider, account_key, patch: captured.update(patch=patch),
    )

    class FakeClient:
        # A change-set too large to page through: partial items, no token.
        async def sync_events(self, cid, *, sync_token=None, time_min=None, time_max=None):
            return [], None

        async def list_events(self, cid, *, time_min=None, time_max=None):
            return []

    async def fake_authorized_client(session, account_key):
        return FakeClient()

    monkeypatch.setattr(discover_mod, "authorized_client", fake_authorized_client)

    await discover_mod.discover_updated_events(
        SimpleNamespace(commit=lambda: None), calendar_ids=["primary"], account_key="acct",
    )

    # The overflowing cursor is dropped so the next run re-baselines within the
    # window rather than replaying the same delta forever.
    assert "primary" not in captured["patch"]["calendar_sync_tokens"]


@pytest.mark.asyncio
async def test_cancelled_event_span_recovery(monkeypatch):
    from app.services.calendar.discover import _cancelled_event_span

    # Recurring instance: the tombstone itself carries originalStartTime.
    span = await _cancelled_event_span(
        None, "cal", {"id": "x", "originalStartTime": {"dateTime": "2026-07-08T12:45:00+02:00"}},
        account_key=None,
    )
    assert span is not None
    assert span[0].isoformat() == "2026-07-08T12:45:00+02:00"

    # Plain deletion: the owner's cancelled copy is fetched and keeps times.
    tombstone = _calendar_event("gone", _day_at(12, 45), _day_at(13, 45))

    async def fake_get_event(session, *, calendar_id, event_id, account_key=None):
        return tombstone

    monkeypatch.setattr("app.services.calendar.get_event", fake_get_event)
    span = await _cancelled_event_span(None, "cal", {"id": "gone"}, account_key=None)
    assert span == (tombstone.start, tombstone.end)

    # Unrecoverable tombstone → None, the daily re-baseline heals instead.
    async def broken_get_event(session, *, calendar_id, event_id, account_key=None):
        raise KeyError("date")

    monkeypatch.setattr("app.services.calendar.get_event", broken_get_event)
    assert await _cancelled_event_span(None, "cal", {"id": "gone"}, account_key=None) is None

    # Recurring instance without originalStartTime: recover the time from the
    # id suffix, never touching the API (`events.get` on such an id can hand
    # back the master, dated at the series origin).
    async def boom_get_event(session, *, calendar_id, event_id, account_key=None):
        raise AssertionError("must not call get_event for a recurring instance id")

    monkeypatch.setattr("app.services.calendar.get_event", boom_get_event)
    span = await _cancelled_event_span(
        None, "cal", {"id": "series_20310206T120000Z"}, account_key=None,
    )
    assert span == (
        datetime(2031, 2, 6, 12, tzinfo=timezone.utc),
        datetime(2031, 2, 6, 12, tzinfo=timezone.utc),
    )


def test_recurring_instance_start_parses_id_suffix():
    from app.services.calendar.discover import _recurring_instance_start

    # Timed instance: basic-ISO UTC suffix.
    assert _recurring_instance_start("26p6u4t7j57iaiqo5pk4rb38dc_20380513T110000Z") == datetime(
        2038, 5, 13, 11, tzinfo=timezone.utc,
    )
    # All-day instance: date-only suffix.
    assert _recurring_instance_start("abc_20380513") == datetime(
        2038, 5, 13, tzinfo=timezone.utc,
    )
    # Plain (non-recurring) event id: no parseable suffix → None, so the caller
    # falls back to fetching the owner's copy.
    assert _recurring_instance_start("plainevent123") is None
    assert _recurring_instance_start("has_underscore_butnotatime") is None


def test_active_events_keep_location_blockers_only():
    from app.services.calendar.discover import _active_events

    def raw(event_id, **kw):
        return {
            "id": event_id, "status": "confirmed", "summary": event_id,
            "start": {"dateTime": _day_at(12).isoformat()},
            "end": {"dateTime": _day_at(16).isoformat()},
            **kw,
        }

    items = [
        raw("bib", location=LIBRARY, transparency="transparent"),
        raw("maybe", transparency="transparent"),
        raw("lecture", location=OFFICE),
        raw("gone", location=OFFICE, status="cancelled"),
    ]

    assert [ev.id for ev in _active_events(items, "primary")] == ["bib", "lecture"]


@pytest.mark.asyncio
async def test_blocker_displaces_only_what_needs_the_user_elsewhere(monkeypatch):
    from app.services.calendar import discover as discover_mod

    async def fake_resolve(location):
        if not location or location.startswith("https://"):
            return None
        return "Homestreet 1" if location == "home" else location

    monkeypatch.setattr(discover_mod, "resolve_routable_location", fake_resolve)
    task_props = {"managed_by": "plan_service", "kind": "task"}
    span = (_day_at(13), _day_at(14))
    bib = _calendar_event(
        "bib", _day_at(12, 30), _day_at(16, 45), location=LIBRARY, transparency="transparent",
    )
    conflicts = [
        _calendar_event("homework", *span, props=task_props),
        _calendar_event("call", *span, location="https://zoom.us/j/1", props=task_props),
        _calendar_event("books", *span, location=LIBRARY.upper(), props=task_props),
        _calendar_event("gym", *span, location=GYM, props=task_props),
        _calendar_event("laundry", *span, location="home", props=task_props),
        _calendar_event(
            "leg", _day_at(12, 40), _day_at(12, 55),
            props={"managed_by": "plan_service", "kind": "commute"},
        ),
    ]

    displaced = await discover_mod._displaced_by_blocker(bib, conflicts)

    assert [ev.id for ev in displaced] == ["gym", "laundry", "leg"]
