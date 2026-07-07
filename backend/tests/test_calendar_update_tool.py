from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.input import runner as input_runner  # noqa: E402
from app.agent.tools import calendar_lookup  # noqa: E402
from app.services.calendar.client import CalendarEvent  # noqa: E402


def _event(
    event_id: str = "event-1",
    *,
    summary: str = "Dentist",
    start: datetime | None = None,
    end: datetime | None = None,
    props: dict[str, str] | None = None,
) -> CalendarEvent:
    start = start or datetime(2026, 7, 10, 14, tzinfo=timezone.utc)
    end = end or start + timedelta(hours=1)
    return CalendarEvent(
        id=event_id,
        calendar_id="primary",
        summary=summary,
        description=None,
        start=start,
        end=end,
        all_day=False,
        location="Clinic",
        html_link="https://calendar.example/event-1",
        private_properties=props or {},
        raw={},
    )


def _response(*tool_calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        message=LLMMessage(role="assistant", tool_calls=tool_calls),
        tool_calls=tool_calls,
        text="",
        stop_reason="tool_use",
        usage={},
        meta={},
        provider="test",
        model="test",
    )


@pytest.mark.asyncio
async def test_find_calendar_events_returns_ids(monkeypatch):
    monkeypatch.setattr(
        calendar_lookup,
        "get_settings",
        lambda: SimpleNamespace(user_timezone="UTC", google_calendar_id="primary"),
    )

    async def fake_list_events_between(*_args, **_kwargs):
        return [_event("evt_123", summary="Talk")]

    monkeypatch.setattr(calendar_lookup, "list_events_between", fake_list_events_between)

    out = await calendar_lookup.run_find_calendar_events(
        SimpleNamespace(),
        "2026-07-10T13:00:00+00:00",
        "2026-07-10T16:00:00+00:00",
    )

    assert "id=evt_123" in out
    assert "Talk" in out


@pytest.mark.asyncio
async def test_update_event_preserves_duration_and_patches_selected_fields(monkeypatch):
    calls = []
    notifications = []
    existing = _event(
        "evt_123",
        start=datetime(2026, 7, 10, 14, tzinfo=timezone.utc),
        end=datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(
        calendar_lookup,
        "get_settings",
        lambda: SimpleNamespace(user_timezone="UTC", google_calendar_id="primary"),
    )

    async def fake_get_event(_session, *, calendar_id, event_id):
        calls.append(("get", calendar_id, event_id))
        return existing

    async def fake_patch_event(_session, **kwargs):
        calls.append(("patch", kwargs))
        return _event(
            kwargs["event_id"],
            summary=kwargs["summary"],
            start=kwargs["start"],
            end=kwargs["end"],
        )

    async def fake_notify(event):
        notifications.append(event.id)

    monkeypatch.setattr(calendar_lookup, "get_event", fake_get_event)
    monkeypatch.setattr(calendar_lookup, "patch_event", fake_patch_event)
    monkeypatch.setattr(calendar_lookup, "notify_calendar_event_updated", fake_notify)

    out, event_id = await calendar_lookup.run_update_event(
        SimpleNamespace(),
        event_id="evt_123",
        summary="Dentist moved",
        start="2026-07-11T10:00:00+00:00",
    )

    patch_kwargs = calls[1][1]
    assert event_id == "evt_123"
    assert "updated 'Dentist moved'" in out
    assert patch_kwargs["calendar_id"] == "primary"
    assert patch_kwargs["event_id"] == "evt_123"
    assert patch_kwargs["summary"] == "Dentist moved"
    assert patch_kwargs["start"].isoformat() == "2026-07-11T10:00:00+00:00"
    assert patch_kwargs["end"].isoformat() == "2026-07-11T11:30:00+00:00"
    assert "description" not in patch_kwargs
    assert notifications == ["evt_123"]


@pytest.mark.asyncio
async def test_update_event_validation_rejects_bad_inputs(monkeypatch):
    monkeypatch.setattr(
        calendar_lookup,
        "get_settings",
        lambda: SimpleNamespace(user_timezone="UTC", google_calendar_id="primary"),
    )
    monkeypatch.setattr(calendar_lookup, "get_event", lambda *_args, **_kwargs: None)

    out, event_id = await calendar_lookup.run_update_event(SimpleNamespace(), event_id="bad id")
    assert event_id is None
    assert "valid calendar event id" in out

    async def fake_get_event(*_args, **_kwargs):
        return _event()

    monkeypatch.setattr(calendar_lookup, "get_event", fake_get_event)

    out, event_id = await calendar_lookup.run_update_event(
        SimpleNamespace(),
        event_id="evt_123",
        start="not-a-date",
    )
    assert event_id is None
    assert "ISO timestamps" in out

    out, event_id = await calendar_lookup.run_update_event(
        SimpleNamespace(),
        event_id="evt_123",
        start="2026-07-10T15:00:00+00:00",
        end="2026-07-10T14:00:00+00:00",
    )
    assert event_id is None
    assert "after `start`" in out


@pytest.mark.asyncio
async def test_update_event_rejects_managed_task_event(monkeypatch):
    patch_calls = []
    managed = _event(props={"managed_by": "plan_service", "kind": "task", "task_id": "t1"})

    monkeypatch.setattr(
        calendar_lookup,
        "get_settings",
        lambda: SimpleNamespace(user_timezone="UTC", google_calendar_id="primary"),
    )

    async def fake_get_event(*_args, **_kwargs):
        return managed

    async def fake_patch_event(*_args, **_kwargs):
        patch_calls.append(_kwargs)
        return managed

    monkeypatch.setattr(calendar_lookup, "get_event", fake_get_event)
    monkeypatch.setattr(calendar_lookup, "patch_event", fake_patch_event)

    out, event_id = await calendar_lookup.run_update_event(
        SimpleNamespace(),
        event_id="evt_123",
        summary="New title",
    )

    assert event_id is None
    assert "use `update_task`" in out
    assert patch_calls == []


@pytest.mark.asyncio
async def test_runner_records_update_event_and_finalizes_as_event(monkeypatch):
    finalized = {}

    async def fake_chat(*_args, **_kwargs):
        return _response(
            ToolCall(
                id="update-event-1",
                name="update_event",
                input={"event_id": "evt_123", "start": "2026-07-11T10:00:00+00:00"},
            ),
            ToolCall(
                id="not-task-1",
                name="mark_not_task",
                input={"reason": "Calendar correction only."},
            ),
        )

    async def fake_update_event(*_args, **_kwargs):
        return "update_event: updated 'Dentist' at 2026-07-11T10:00:00+00:00.", "evt_123"

    def fake_finalize(_session, raw_id, **kwargs):
        finalized.update({"raw_id": raw_id, **kwargs})

    monkeypatch.setattr(input_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(input_runner, "chat", fake_chat)
    monkeypatch.setattr(input_runner, "run_update_event", fake_update_event)
    monkeypatch.setattr(input_runner.raw_inputs, "finalize", fake_finalize)

    raw = SimpleNamespace(
        id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
        source="manual",
        source_metadata={},
        content="Move my dentist appointment to Friday at 10.",
    )
    session = SimpleNamespace(commit=lambda: None)

    trace = await input_runner.run_new_input_agent(session, raw, [], None)

    assert trace["events_updated"] == ["evt_123"]
    assert finalized["status"] == "event"
    assert finalized["task_id"] is None
    update_result = trace["iterations"][0]["tool_results"][0]
    assert update_result["changed_state"] is True
    assert update_result["artifact_refs"] == ["event:evt_123"]
