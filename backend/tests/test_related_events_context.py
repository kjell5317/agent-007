from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent import retrieval  # noqa: E402
from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.input import runner as input_runner  # noqa: E402
from app.agent.manual import runner as manual_runner  # noqa: E402
from app.agent.retrieval import related_calendar_events  # noqa: E402 — real impl; conftest stubs the attribute
from app.agent.tools.calendar_lookup import related_events_section  # noqa: E402
from app.db.clients.documents import CalendarMatch  # noqa: E402
from app.services import event_context  # noqa: E402


def _match(event_id: str, summary: str, starts_at: datetime | None, **overrides) -> CalendarMatch:
    values = {
        "event_id": event_id,
        "calendar_id": "primary",
        "summary": summary,
        "location": None,
        "starts_at": starts_at,
        "similarity": 0.8,
        "description": None,
    }
    values.update(overrides)
    return CalendarMatch(**values)


def _response(tool_call: ToolCall) -> LLMResponse:
    return LLMResponse(
        message=LLMMessage(role="assistant", tool_calls=(tool_call,)),
        tool_calls=(tool_call,),
        text="",
        stop_reason="tool_use",
        usage={},
        meta={},
        provider="test",
        model="test",
    )


def test_soonest_per_event_collapses_recurring_instances():
    base = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
    standup_later = _match("s2", "Standup", base + timedelta(days=7))
    trip = _match("m1", "Mexico trip", base + timedelta(days=40))
    standup_soon = _match("s1", "Standup", base)

    result = retrieval.soonest_per_event([standup_later, trip, standup_soon])

    assert [m.event_id for m in result] == ["s1", "m1"]


@pytest.mark.asyncio
async def test_related_calendar_events_reuses_embedding_and_limits(monkeypatch):
    calls: dict = {}

    async def no_embed(text):
        raise AssertionError("must reuse the given embedding")

    def fake_search(session, **kwargs):
        calls.update(kwargs)
        base = datetime(2026, 12, 1, tzinfo=timezone.utc)
        return [
            _match("a", "Standup", base + timedelta(days=7)),
            _match("b", "Standup", base),
            _match("c", "Mexico trip", base + timedelta(days=9)),
            _match("d", "Dentist", base + timedelta(days=3)),
        ]

    monkeypatch.setattr(
        retrieval,
        "get_settings",
        lambda: SimpleNamespace(task_related_events_limit=2, task_related_events_min_similarity=0.75),
    )
    monkeypatch.setattr(retrieval, "embed", no_embed)
    monkeypatch.setattr(retrieval.documents_store, "search_calendar_semantic", fake_search)
    raw = SimpleNamespace(
        content="Please complete your booking", source_metadata={"subject": "Oaxaca stay"}
    )

    result = await related_calendar_events(object(), raw=raw, embedding=[0.1, 0.2])

    assert [m.event_id for m in result] == ["b", "c"]
    assert calls["embedding"] == [0.1, 0.2]
    assert calls["raw_text"] == "Oaxaca stay"
    assert calls["k"] == 8
    assert calls["min_similarity"] == 0.75
    assert datetime.fromisoformat(calls["time_min"]) <= datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_related_calendar_events_embeds_input_without_stored_embedding(monkeypatch):
    embedded: list[str] = []
    searched: dict = {}

    async def fake_embed(text):
        embedded.append(text)
        return [0.3]

    def fake_search(session, **kwargs):
        searched.update(kwargs)
        return []

    monkeypatch.setattr(
        retrieval,
        "get_settings",
        lambda: SimpleNamespace(task_related_events_limit=3, task_related_events_min_similarity=0.75),
    )
    monkeypatch.setattr(retrieval, "embed", fake_embed)
    monkeypatch.setattr(retrieval.documents_store, "search_calendar_semantic", fake_search)
    raw = SimpleNamespace(content="Book Mexico accommodation", source_metadata={})

    assert await related_calendar_events(object(), raw=raw) == []
    assert embedded == ["Book Mexico accommodation"]
    assert searched["embedding"] == [0.3]


def test_related_events_section_lists_ids_in_local_time_with_short_details():
    starts = datetime(2026, 12, 10, 15, tzinfo=timezone.utc)
    match = _match(
        "evt1", "Mexico trip", starts,
        location="Mexico City",
        description="Mexico trip\nMexico City\n" + "x" * 400,
    )

    lines = related_events_section([match], ZoneInfo("Europe/Berlin"))

    assert related_events_section([], ZoneInfo("UTC")) == []
    assert "Upcoming calendar events that may be related" in lines[1]
    assert lines[2].startswith(
        "- id=evt1 | calendar_id=primary | 2026-12-10T16:00:00+01:00 | Mexico trip @ Mexico City"
    )
    assert lines[2].endswith("x" * 300 + " [truncated]")


def test_new_input_message_places_related_events_before_body(monkeypatch):
    monkeypatch.setattr(input_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    raw = SimpleNamespace(source="gmail", source_metadata={}, content="Complete your booking")
    match = _match("evt1", "Mexico trip", datetime(2026, 12, 10, tzinfo=timezone.utc))

    message = input_runner._build_new_input_message(raw, [], [], [match])

    assert message.index("- id=evt1 | calendar_id=primary") < message.index("Body:")
    assert "Upcoming calendar events" not in input_runner._build_new_input_message(raw, [], [])


@pytest.mark.asyncio
async def test_extract_task_fields_links_listed_event_and_due_date_precedes_it(monkeypatch):
    event_start = (datetime.now(timezone.utc) + timedelta(days=60)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    match = _match("mex1", "Mexico trip", event_start)
    seen: dict = {}

    async def fake_related(session, *, raw, embedding=None):
        seen["embedding"] = embedding
        return [match]

    async def fake_chat(messages, settings, *, system_prompt, tools, force_tool=None):
        seen["first_message"] = messages[0].text
        return _response(ToolCall(
            id="create-1",
            name="create_task",
            input={
                "title": "Book Mexico accommodation",
                "estimation": 45,
                # The model ignored the trip date; the link must still pull it in.
                "due_date": (event_start + timedelta(days=5)).isoformat(),
                "label": "personal",
                "related_event_id": "mex1",
                "related_event_calendar_id": "primary",
            },
        ))

    monkeypatch.setattr(manual_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(manual_runner, "chat", fake_chat)
    monkeypatch.setattr(retrieval, "related_calendar_events", fake_related)
    monkeypatch.setattr(
        event_context.documents_store,
        "get_by_external_id",
        lambda session, provider, external_id: (
            SimpleNamespace(starts_at=event_start) if external_id == "primary:mex1" else None
        ),
    )
    raw = SimpleNamespace(
        id="raw-1",
        source="manual",
        source_metadata={},
        content="Book Mexico accommodation",
        received_at=datetime.now(timezone.utc),
        embedding=[0.5],
    )

    payload, trace = await manual_runner.extract_task_fields(
        SimpleNamespace(), raw, include_trace=True
    )

    assert seen["embedding"] == [0.5]
    assert "- id=mex1 | calendar_id=primary" in seen["first_message"]
    assert trace["related_events"][0]["event_id"] == "mex1"
    assert payload["related_event_id"] == "mex1"
    assert payload["related_event_due_derived"] is True
    assert payload["due_date"] < event_start
