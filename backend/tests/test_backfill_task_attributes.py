from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.scripts.backfill_task_attributes import (  # noqa: E402
    CLASSIFY_TOOL,
    batch_message,
    parse_classification,
    planned_update,
)


def test_parse_classification_keeps_valid_entries_for_listed_numbers():
    tool_input = {"tasks": [
        {"n": 1, "work_type": "physical", "effort": "low", "deadline_kind": "soft"},
        {"n": "2", "work_type": "Cognitive", "effort": "extreme", "deadline_kind": "hard"},
        {"n": 7, "work_type": "social", "effort": "low", "deadline_kind": "firm"},
        {"n": "x", "work_type": "social"},
        {"n": 3, "work_type": "nope"},
        "garbage",
    ]}

    assert parse_classification(tool_input, count=3) == {
        1: {"work_type": "physical", "effort": "low", "deadline_kind": "soft"},
        2: {"work_type": "cognitive", "deadline_kind": "hard"},
    }


def test_parse_classification_tolerates_a_missing_or_malformed_call():
    assert parse_classification(None, count=3) == {}
    assert parse_classification({"tasks": "nope"}, count=3) == {}


def test_label_defaults_beat_the_model_and_only_missing_fields_fill():
    suggested = {"work_type": "cognitive", "effort": "high", "deadline_kind": "soft"}

    assert planned_update(
        {"work_type": None, "effort": None, "deadline_kind": "firm"},
        suggested, ("physical", None), deadline_kind_overridden=False,
    ) == {"work_type": "physical", "effort": "high", "deadline_kind": "soft"}
    assert planned_update(
        {"work_type": "social", "effort": None, "deadline_kind": "firm"},
        suggested, (None, None), deadline_kind_overridden=False,
    ) == {"effort": "high", "deadline_kind": "soft"}


def test_deadline_kind_changes_only_while_it_is_the_untouched_default():
    suggested = {"deadline_kind": "hard"}
    current = {"work_type": "social", "effort": "low", "deadline_kind": "firm"}

    assert planned_update(current, suggested, (None, None), deadline_kind_overridden=True) == {}
    assert planned_update(
        {**current, "deadline_kind": "soft"}, suggested, (None, None),
        deadline_kind_overridden=False,
    ) == {}
    assert planned_update(
        current, {"deadline_kind": "firm"}, (None, None), deadline_kind_overridden=False
    ) == {}


def test_batch_message_numbers_tasks_and_marks_derived_due_dates():
    message = batch_message([
        {
            "title": "Book flights",
            "label": "Reise",
            "estimation": 30,
            "due_date": datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
            "due_date_derived": True,
            "related_event": True,
            "description": "For the\nLisbon trip",
        },
        {"title": "Call mum", "due_date": None},
    ])

    assert message.splitlines() == [
        "1. Book flights",
        "   label: Reise · estimate: 30 min · due: 2026-07-01T09:00+00:00 (derived) · linked event",
        "   description: For the Lisbon trip",
        "2. Call mum",
    ]


def test_classify_tool_requires_every_field_per_task():
    item = CLASSIFY_TOOL["parameters"]["properties"]["tasks"]["items"]

    assert item["required"] == ["n", "work_type", "effort", "deadline_kind"]
    assert item["properties"]["deadline_kind"]["enum"] == ["hard", "firm", "soft"]
