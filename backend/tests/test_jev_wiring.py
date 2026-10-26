"""Jev triage in front of the agents: the orchestrator and the manual queue."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent import orchestrator  # noqa: E402
from app.agent.triage import runner as triage  # noqa: E402
from app.services.task import queue as task_queue  # noqa: E402

ACTING = SimpleNamespace(
    typesafe_api_key="ts-key",
    jev_mode="on",
    input_dedup_threshold=0.88,
)
OFF = SimpleNamespace(typesafe_api_key="", jev_mode="off", input_dedup_threshold=0.88)


def _task(task_id):
    return SimpleNamespace(id=task_id, title="Send report", parent_task_id=None, is_container=False)


def _thread_setup(monkeypatch, settings):
    task = _task(uuid.uuid4())
    raw = SimpleNamespace(
        id=uuid.uuid4(),
        source="gmail",
        source_metadata={"thread_id": "t1"},
        processed_at=None,
    )
    calls = {"agent": [], "triage": []}

    async def no_shortcut(*_args):
        return None

    async def agent(_session, _raw, _task, **kwargs):
        calls["agent"].append(kwargs)
        return {"outcome": "updated"}

    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings)
    monkeypatch.setattr(orchestrator.raw_inputs, "get", lambda *_args: raw)
    monkeypatch.setattr(
        orchestrator.raw_inputs,
        "find_by_thread",
        lambda *_a, **_k: SimpleNamespace(id=uuid.uuid4(), task_id=task.id),
    )
    monkeypatch.setattr(orchestrator.tasks, "get", lambda _s, _id: task)
    monkeypatch.setattr(orchestrator, "_github_email_shortcut", no_shortcut)
    monkeypatch.setattr(orchestrator, "run_thread_followup", agent)
    return raw, task, calls


@pytest.mark.asyncio
async def test_thread_followup_decided_by_jev_skips_the_agent(monkeypatch):
    raw, task, calls = _thread_setup(monkeypatch, ACTING)

    async def decided(_session, _raw, _task):
        calls["triage"].append(_task.id)
        return triage.Outcome(trace={"outcome": "no_change", "branch": "jev_followup"})

    monkeypatch.setattr(orchestrator.triage, "followup", decided)
    result = await orchestrator._process_raw_input(SimpleNamespace(), raw.id)

    assert result["branch"] == "jev_followup"
    assert calls == {"agent": [], "triage": [task.id]}


@pytest.mark.asyncio
async def test_thread_followup_hands_jev_answers_to_the_agent(monkeypatch):
    raw, _task_row, calls = _thread_setup(monkeypatch, ACTING)

    async def deferred(*_args):
        return triage.Outcome(jev={"decision": "llm"})

    monkeypatch.setattr(orchestrator.triage, "followup", deferred)
    await orchestrator._process_raw_input(SimpleNamespace(), raw.id)
    assert calls["agent"] == [{"jev": {"decision": "llm"}}]


@pytest.mark.asyncio
async def test_without_jev_the_agent_runs_as_before(monkeypatch):
    raw, _task_row, calls = _thread_setup(monkeypatch, OFF)

    async def never(*_args):
        raise AssertionError("triage must not run when Jev is off")

    monkeypatch.setattr(orchestrator.triage, "followup", never)
    await orchestrator._process_raw_input(SimpleNamespace(), raw.id)
    assert calls["agent"] == [{}]


@pytest.mark.asyncio
async def test_new_input_goes_through_triage_then_the_agent(monkeypatch):
    raw = SimpleNamespace(
        id=uuid.uuid4(),
        source="gmail",
        source_metadata={},
        processed_at=None,
        embedding=None,
        content="Hello",
    )
    seen = {}

    async def no_shortcut(*_args):
        return None

    async def events(_session, *, raw, embedding=None):
        return ["event"]

    async def deferred(_session, _raw, candidates, related_events):
        seen["triage_events"] = related_events
        return triage.Outcome(jev={"decision": "llm"})

    async def agent(_session, _raw, candidates, embedding, **kwargs):
        seen["agent"] = kwargs
        return {"outcome": "task_created"}

    monkeypatch.setattr(orchestrator, "get_settings", lambda: ACTING)
    monkeypatch.setattr(orchestrator.raw_inputs, "get", lambda *_args: raw)
    monkeypatch.setattr(orchestrator, "_github_email_shortcut", no_shortcut)
    monkeypatch.setattr(orchestrator.retrieval, "related_calendar_events", events)
    monkeypatch.setattr(orchestrator.triage, "new_input", deferred)
    monkeypatch.setattr(orchestrator, "run_new_input_agent", agent)

    await orchestrator._process_raw_input(SimpleNamespace(), raw.id)
    assert seen["triage_events"] == ["event"]
    assert seen["agent"] == {"related_events": ["event"], "jev": {"decision": "llm"}}


@pytest.mark.asyncio
async def test_manual_fast_path_creates_the_task_without_the_extractor(monkeypatch):
    raw_id = uuid.uuid4()
    raw = SimpleNamespace(
        id=raw_id,
        source="manual",
        content="Buy printer paper",
        source_metadata={"manual": True},
        embedding=[0.1],
        task_id=None,
        processed_at=None,
        agent_trace=None,
        status="processing",
    )
    session = SimpleNamespace(commit=lambda: None)
    created, harvested = [], []

    class FakeSessionLocal:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return None

    async def manual(_session, _raw, _user_fields):
        return triage.ManualOutcome(
            fields={
                "title": "Buy printer paper",
                "label": "Errands",
                "estimation": 15,
                "estimation_source": "jev",
                "due_date": datetime(2026, 10, 30, 23, 45, tzinfo=timezone.utc),
            },
            trace={"branch": "manual", "agent": "jev", "jev": {"decision": "create"}},
            facts=True,
        )

    async def extractor(*_args, **_kwargs):
        raise AssertionError("the extraction agent must not run")

    async def harvest(_session, _raw, settings=None):
        harvested.append(_raw.id)
        return []

    async def nothing(*_args, **_kwargs):
        return []

    def create(_session, payload):
        created.append(payload)
        return SimpleNamespace(
            id=uuid.uuid4(), due_date=payload.due_date, scheduled_date=None, calendar_event_id=None
        )

    monkeypatch.setattr(task_queue, "get_settings", lambda: ACTING)
    monkeypatch.setattr(task_queue, "SessionLocal", FakeSessionLocal)
    monkeypatch.setattr(task_queue.raw_inputs_store, "get", lambda *_args: raw)
    monkeypatch.setattr(triage, "manual", manual)
    monkeypatch.setattr(triage, "harvest_notes", harvest)
    monkeypatch.setattr(task_queue, "extract_task_fields", extractor)
    monkeypatch.setattr(task_queue.tasks_store, "create", create)
    monkeypatch.setattr(task_queue, "maybe_split_task", nothing)
    monkeypatch.setattr(task_queue, "schedule_task", nothing)
    monkeypatch.setattr(task_queue, "publish_task", lambda *_args: None)
    monkeypatch.setattr(task_queue, "publish_input", lambda *_args: None)

    await task_queue._process(raw_id, {}, [])

    assert created[0].title == "Buy printer paper"
    assert created[0].estimation_source == "jev"
    assert raw.status == "open"
    assert raw.agent_trace["agent"] == "jev"
    assert harvested == [raw_id]


@pytest.mark.asyncio
async def test_manual_event_cue_keeps_the_extractor(monkeypatch):
    raw_id = uuid.uuid4()
    raw = SimpleNamespace(
        id=raw_id,
        source="manual",
        content="Prepare slides for the meeting",
        source_metadata={"manual": True},
        embedding=[0.1],
        task_id=None,
        processed_at=None,
        agent_trace=None,
        status="processing",
    )

    class FakeSessionLocal:
        def __enter__(self):
            return SimpleNamespace(commit=lambda: None)

        def __exit__(self, *_args):
            return None

    async def never(*_args, **_kwargs):
        raise AssertionError("event cues stay with the agent")

    async def extractor(*_args, **_kwargs):
        return {"title": "Prepare slides", "estimation": 60}, {"branch": "manual"}

    async def nothing(*_args, **_kwargs):
        return []

    monkeypatch.setattr(task_queue, "get_settings", lambda: ACTING)
    monkeypatch.setattr(task_queue, "SessionLocal", FakeSessionLocal)
    monkeypatch.setattr(task_queue.raw_inputs_store, "get", lambda *_args: raw)
    monkeypatch.setattr(triage, "manual", never)
    monkeypatch.setattr(task_queue, "_collect_extraction_precedents", nothing)
    monkeypatch.setattr(task_queue, "extract_task_fields", extractor)
    monkeypatch.setattr(
        task_queue.tasks_store,
        "create",
        lambda _s, payload: SimpleNamespace(id=uuid.uuid4(), due_date=None),
    )
    monkeypatch.setattr(task_queue, "maybe_split_task", nothing)
    monkeypatch.setattr(task_queue, "schedule_task", nothing)
    monkeypatch.setattr(task_queue, "publish_task", lambda *_args: None)
    monkeypatch.setattr(task_queue, "publish_input", lambda *_args: None)

    await task_queue._process(raw_id, {}, [])
    assert raw.status == "open"
