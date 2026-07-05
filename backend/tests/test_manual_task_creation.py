from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.task import create as create_svc  # noqa: E402
from app.services.task import queue as task_queue  # noqa: E402


class FakeSession:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, row):
        if getattr(row, "id", None) is None:
            row.id = uuid.UUID("10000000-0000-0000-0000-000000000001")
        if getattr(row, "received_at", None) is None:
            row.received_at = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def refresh(self, _row):
        return None


@pytest.mark.asyncio
async def test_manual_composer_content_is_not_enqueued_as_title(monkeypatch):
    enqueued = []
    published = []

    async def fake_enqueue(raw_input_id, user_fields):
        enqueued.append((raw_input_id, user_fields))

    monkeypatch.setattr(create_svc, "enqueue", fake_enqueue)
    monkeypatch.setattr(create_svc, "publish_input", lambda _session, raw_id: published.append(raw_id))

    session = FakeSession()
    raw = await create_svc.create_manual_task(
        session,
        {"content": "30m tomorrow: check the quarterly forecast"},
    )

    assert raw.content == "30m tomorrow: check the quarterly forecast"
    assert raw.source_metadata == {"manual": True}
    assert enqueued == [(raw.id, {})]
    assert published == [raw.id]


@pytest.mark.asyncio
async def test_manual_queue_uses_extracted_title_when_no_structured_title(monkeypatch):
    raw_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
    raw = SimpleNamespace(
        id=raw_id,
        task_id=None,
        processed_at=None,
        agent_trace=None,
        status="processing",
    )
    session = SimpleNamespace(commit=lambda: None)
    created_payloads = []

    class FakeSessionLocal:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return None

    async def fake_extract_task_fields(_session, _raw, *, context_inputs, include_trace):
        assert include_trace is True
        return {
            "title": "Check quarterly forecast",
            "estimation": 30,
            "due_date": datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc),
        }, {
            "branch": "manual",
            "iterations": [
                {
                    "blocks": [
                        {
                            "type": "tool_use",
                            "id": "create-1",
                            "name": "create_task",
                            "input": {"title": "Check quarterly forecast"},
                        }
                    ],
                    "llm": {
                        "provider": "test",
                        "model": "test-model",
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                    "tool_results": [
                        {
                            "name": "create_task",
                            "status": "success",
                            "purpose": "create task Check quarterly forecast",
                            "preview": "extracted task fields",
                            "result_summary": "extracted task fields",
                            "changed_state": False,
                            "artifact_refs": [],
                        }
                    ],
                }
            ],
        }

    def fake_create(_session, payload):
        created_payloads.append(payload)
        return SimpleNamespace(id=uuid.UUID("30000000-0000-0000-0000-000000000001"))

    async def fake_schedule_task(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_queue, "SessionLocal", FakeSessionLocal)
    monkeypatch.setattr(task_queue.raw_inputs_store, "get", lambda _session, _raw_id: raw)
    monkeypatch.setattr(task_queue, "extract_task_fields", fake_extract_task_fields)
    monkeypatch.setattr(task_queue.tasks_store, "create", fake_create)
    monkeypatch.setattr(task_queue, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(task_queue, "publish_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_queue, "publish_input", lambda *_args, **_kwargs: None)

    await task_queue._process(raw_id, {}, [])

    assert created_payloads[0].title == "Check quarterly forecast"
    assert raw.status == "open"
    assert raw.agent_trace["branch"] == "manual"
    assert raw.agent_trace["outcome"] == "task_created"
    assert raw.agent_trace["task_id"] == "30000000-0000-0000-0000-000000000001"
    assert raw.agent_trace["agent_extracted"] == ["due_date", "estimation", "title"]
    assert raw.agent_trace["user_provided"] == []
    assert raw.agent_trace["iterations"][0]["llm"]["model"] == "test-model"
    create_result = raw.agent_trace["iterations"][0]["tool_results"][0]
    assert create_result["changed_state"] is True
    assert create_result["artifact_refs"] == [
        "task:30000000-0000-0000-0000-000000000001"
    ]


@pytest.mark.asyncio
async def test_manual_queue_structured_title_overrides_extracted_title(monkeypatch):
    raw_id = uuid.UUID("20000000-0000-0000-0000-000000000002")
    raw = SimpleNamespace(
        id=raw_id,
        task_id=None,
        processed_at=None,
        agent_trace=None,
        status="processing",
    )
    session = SimpleNamespace(commit=lambda: None)
    created_payloads = []

    class FakeSessionLocal:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return None

    async def fake_extract_task_fields(_session, _raw, *, context_inputs, include_trace):
        assert include_trace is True
        return {
            "title": "Extracted title",
            "estimation": 45,
            "due_date": datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc),
        }, {"branch": "manual", "iterations": []}

    def fake_create(_session, payload):
        created_payloads.append(payload)
        return SimpleNamespace(id=uuid.UUID("30000000-0000-0000-0000-000000000002"))

    async def fake_schedule_task(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_queue, "SessionLocal", FakeSessionLocal)
    monkeypatch.setattr(task_queue.raw_inputs_store, "get", lambda _session, _raw_id: raw)
    monkeypatch.setattr(task_queue, "extract_task_fields", fake_extract_task_fields)
    monkeypatch.setattr(task_queue.tasks_store, "create", fake_create)
    monkeypatch.setattr(task_queue, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(task_queue, "publish_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_queue, "publish_input", lambda *_args, **_kwargs: None)

    await task_queue._process(raw_id, {"title": "Explicit title"}, [])

    assert created_payloads[0].title == "Explicit title"


@pytest.mark.asyncio
async def test_manual_queue_preserves_prior_trace_under_manual_override(monkeypatch):
    raw_id = uuid.UUID("20000000-0000-0000-0000-000000000003")
    prior_trace = {"outcome": "not_task", "reason": "FYI only"}
    raw = SimpleNamespace(
        id=raw_id,
        task_id=None,
        processed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
        agent_trace=prior_trace,
        status="not_task",
    )
    session = SimpleNamespace(commit=lambda: None)

    class FakeSessionLocal:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return None

    async def fake_extract_task_fields(_session, _raw, *, context_inputs, include_trace):
        assert include_trace is True
        return {
            "title": "Override task",
            "estimation": 25,
            "due_date": datetime(2026, 7, 4, 9, 0, tzinfo=timezone.utc),
        }, {
            "branch": "manual",
            "iterations": [
                {
                    "blocks": [
                        {
                            "type": "tool_use",
                            "id": "search-1",
                            "name": "search_notes",
                            "input": {"query": "override"},
                        },
                        {
                            "type": "tool_use",
                            "id": "create-1",
                            "name": "create_task",
                            "input": {"title": "Override task"},
                        },
                    ],
                    "tool_results": [
                        {
                            "name": "search_notes",
                            "status": "success",
                            "result_summary": "found note",
                        },
                        {
                            "name": "create_task",
                            "status": "success",
                            "artifact_refs": [],
                        },
                    ],
                }
            ],
        }

    def fake_create(_session, _payload):
        return SimpleNamespace(id=uuid.UUID("30000000-0000-0000-0000-000000000003"))

    async def fake_schedule_task(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_queue, "SessionLocal", FakeSessionLocal)
    monkeypatch.setattr(task_queue.raw_inputs_store, "get", lambda _session, _raw_id: raw)
    monkeypatch.setattr(task_queue, "extract_task_fields", fake_extract_task_fields)
    monkeypatch.setattr(task_queue.tasks_store, "create", fake_create)
    monkeypatch.setattr(task_queue, "schedule_task", fake_schedule_task)
    monkeypatch.setattr(task_queue, "publish_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_queue, "publish_input", lambda *_args, **_kwargs: None)

    await task_queue._process(raw_id, {}, [])

    assert raw.agent_trace["outcome"] == "not_task"
    assert raw.agent_trace["reason"] == "FYI only"
    override = raw.agent_trace["manual_override"]
    assert override["branch"] == "manual"
    assert override["outcome"] == "task_created"
    assert override["task_id"] == "30000000-0000-0000-0000-000000000003"
    assert override["iterations"][0]["tool_results"][0]["name"] == "search_notes"
    assert override["iterations"][0]["tool_results"][1]["artifact_refs"] == [
        "task:30000000-0000-0000-0000-000000000003"
    ]
