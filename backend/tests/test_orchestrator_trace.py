from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent import orchestrator  # noqa: E402
from app.db.clients.raw_inputs import SimilarInput  # noqa: E402


def test_auto_decision_evidence_ref_is_compact_and_readable():
    hit = SimilarInput(
        id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        source="slack",
        status="open",
        task_id=uuid.UUID("10000000-0000-0000-0000-000000000002"),
        similarity=0.93456,
        decayed_similarity=0.91234,
        agent_trace=None,
        subject=None,
        sender="Ada",
        content_snippet="Please send the launch checklist.\nThanks.",
        received_at=datetime(2026, 6, 2, 8, 15, tzinfo=timezone.utc),
    )

    ref = orchestrator._evidence_ref(hit, selected=True)

    assert ref == {
        "ref": "precedent:00000000-0000-0000-0000-000000000002",
        "kind": "precedent",
        "id": "00000000-0000-0000-0000-000000000002",
        "status": "open",
        "source": "slack",
        "task_id": "10000000-0000-0000-0000-000000000002",
        "similarity": 0.9346,
        "decayed_similarity": 0.9123,
        "title": "Please send the launch checklist.",
        "snippet": "Please send the launch checklist. Thanks.",
        "sender": "Ada",
        "received_at": "2026-06-02T08:15:00+00:00",
        "selected": True,
    }


def test_auto_decision_title_falls_back_to_sender_without_placeholder():
    hit = SimilarInput(
        id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        source="gmail",
        status="not_task",
        task_id=None,
        similarity=0.9,
        decayed_similarity=0.9,
        agent_trace=None,
        subject=None,
        sender="reader@example.com",
        content_snippet="",
        received_at=datetime(2026, 6, 2, 8, 15, tzinfo=timezone.utc),
    )

    ref = orchestrator._evidence_ref(hit)

    assert ref["title"] == "gmail from reader@example.com"
