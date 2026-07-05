from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.input import runner  # noqa: E402
from app.agent.prompts import NEW_INPUT_SYSTEM_PROMPT, THREAD_FOLLOWUP_SYSTEM_PROMPT  # noqa: E402
from app.agent.tools.schemas import NEW_INPUT_TOOLS  # noqa: E402
from app.db.clients.raw_inputs import SimilarInput  # noqa: E402


def _hit(**overrides) -> SimilarInput:
    values = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "source": "gmail",
        "status": "not_task",
        "task_id": None,
        "label": None,
        "similarity": 0.8123,
        "decayed_similarity": 0.8123,
        "agent_trace": {"reason": "Informational newsletter with no action requested."},
        "subject": "Weekly FYI",
        "sender": "sender@example.com",
        "content_snippet": "Hello,\n\nHere is an FYI update with details for context.",
        "received_at": datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimilarInput(**values)


def test_new_input_prompt_renders_task_candidate_header_without_duplicate_title(monkeypatch):
    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))

    raw = SimpleNamespace(
        source="manual",
        source_metadata={},
        content="Please send the Q2 report.",
    )
    task_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    task = SimpleNamespace(
        id=task_id,
        title="Send Q2 report",
        description="Send the final Q2 report to finance.",
        due_date=None,
        estimation=20,
        location=None,
        link=None,
        label="admin",
    )
    hit = _hit(
        status="open",
        task_id=task_id,
        similarity=0.91,
        subject="Q2 report",
        content_snippet="Please send the Q2 report.",
    )

    message = runner._build_new_input_message(raw, [(hit, task)], [])

    assert (
        "[OPEN] sim=0.91 · existing_task_id=10000000-0000-0000-0000-000000000001 "
        "· title: Send Q2 report"
    ) in message
    assert "  description: Send the final Q2 report to finance." in message
    assert "  estimation: 20 min" in message
    assert "  label: admin" in message
    assert message.count("title: Send Q2 report") == 1


def test_new_input_prompt_renders_not_task_context(monkeypatch):
    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))

    raw = SimpleNamespace(
        source="gmail",
        source_metadata={"from": "requester@example.com", "subject": "Current item"},
        content="Can you check this?",
    )
    not_task = _hit(
        content_snippet=(
            "Hello,\n\n"
            "This is a context-heavy informational update that matched the current item.\n"
            "There is no requested action."
        ),
    )

    message = runner._build_new_input_message(raw, [], [not_task])

    assert "[NOT_TASK] sim=0.81 · title: Weekly FYI" in message
    assert "  id: 00000000-0000-0000-0000-000000000001" in message
    assert (
        "  metadata: source=gmail · from=sender@example.com · "
        "received_at=2026-06-01T12:30:00+00:00"
    ) in message
    assert "  snippet: Hello, This is a context-heavy informational update" in message
    assert "  reason: Informational newsletter with no action requested." in message


def test_not_task_title_falls_back_to_first_content_line():
    hit = _hit(subject=None, content_snippet="\n\nFirst meaningful line\nSecond line")

    assert runner._candidate_title(hit) == "First meaningful line"


def test_candidate_title_falls_back_to_sender_without_placeholder():
    hit = _hit(subject=None, content_snippet="", sender="Ada Lovelace")

    assert runner._candidate_title(hit) == "gmail from Ada Lovelace"


def test_candidate_trace_ref_includes_readable_evidence_fields():
    hit = _hit(label="admin")

    ref = runner._candidate_trace_ref(hit)

    assert ref == {
        "ref": "candidate:00000000-0000-0000-0000-000000000001",
        "kind": "candidate",
        "id": "00000000-0000-0000-0000-000000000001",
        "status": "not_task",
        "source": "gmail",
        "task_id": None,
        "label": "admin",
        "similarity": 0.8123,
        "sim": 0.8123,
        "title": "Weekly FYI",
        "snippet": "Hello, Here is an FYI update with details for context.",
        "sender": "sender@example.com",
        "received_at": "2026-06-01T12:30:00+00:00",
    }


def test_tool_result_entry_records_status_purpose_and_artifacts():
    entry = runner._tool_result_entry(
        "create_task",
        {"title": "Send Q2 report", "api_key": "secret"},
        "created task 10000000-0000-0000-0000-000000000001",
        changed_state=True,
        artifact_refs=["task:10000000-0000-0000-0000-000000000001"],
    )

    assert entry["name"] == "create_task"
    assert entry["status"] == "success"
    assert entry["purpose"] == "create task Send Q2 report"
    assert entry["changed_state"] is True
    assert entry["artifact_refs"] == ["task:10000000-0000-0000-0000-000000000001"]
    assert entry["result_summary"] == "created task 10000000-0000-0000-0000-000000000001"


def test_create_task_schema_requires_displayable_title():
    create_task = next(tool for tool in NEW_INPUT_TOOLS if tool["name"] == "create_task")
    title = create_task["parameters"]["properties"]["title"]

    assert "title" in create_task["parameters"]["required"]
    assert title["minLength"] == 3
    assert "No subject" in title["description"]


def test_reopen_prompt_guidance_requires_future_due_date_for_past_task():
    for prompt in (NEW_INPUT_SYSTEM_PROMPT, THREAD_FOLLOWUP_SYSTEM_PROMPT):
        assert (
            "When reopening a closed task whose current `due_date` or `scheduled_date`"
            in prompt
        )
        assert "include a new future `due_date`" in prompt


def test_update_task_schema_mentions_reopen_with_past_due_date_rule():
    update_task = next(tool for tool in NEW_INPUT_TOOLS if tool["name"] == "update_task")
    props = update_task["parameters"]["properties"]

    assert (
        "current due_date or scheduled_date is in the past"
        in props["status"]["description"]
    )
    assert "new future due_date" in props["due_date"]["description"]
