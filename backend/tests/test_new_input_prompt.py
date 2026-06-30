from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.input import runner  # noqa: E402
from app.db.clients.raw_inputs import SimilarInput  # noqa: E402


def _hit(**overrides) -> SimilarInput:
    values = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "source": "gmail",
        "status": "not_task",
        "task_id": None,
        "similarity": 0.8123,
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
