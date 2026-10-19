from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.scripts.backfill_overrides import (  # noqa: E402
    AgentCall,
    chat_calls,
    override_rows,
    trace_calls,
)

TASK = "10000000-0000-0000-0000-000000000001"
SUBTASK = "10000000-0000-0000-0000-000000000002"
RAW_1 = uuid.UUID("20000000-0000-0000-0000-000000000001")
RAW_2 = uuid.UUID("20000000-0000-0000-0000-000000000002")


def _use(name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": f"{name}-1", "name": name, "input": tool_input}


def test_only_the_executed_terminal_call_of_a_trace_counts():
    trace = {
        "task_id": TASK,
        "iterations": [
            # Asked again after the link research, so this one never ran.
            {"blocks": [_use("research_input_link"), _use("create_task", estimation=10)]},
            {"blocks": [{"type": "text", "text": "ok"}, _use("create_task", estimation=30)]},
        ],
    }

    calls = trace_calls(trace, raw_input_id=RAW_1)

    assert calls == [AgentCall(TASK, {"estimation": 30}, RAW_1)]


def test_non_task_terminal_and_lookups_yield_nothing():
    trace = {"iterations": [{"blocks": [_use("search_notes"), _use("mark_not_task")]}]}

    assert trace_calls(trace, raw_task_id=TASK) == []
    assert trace_calls(None) == []


def test_update_targets_follow_the_named_task_and_manual_overrides_nest():
    thread_trace = {
        "task_id": TASK,
        "blocks": [_use("update_task", existing_task_id=SUBTASK, estimation=20)],
        "manual_override": {
            "task_id": TASK,
            "iterations": [{"blocks": [_use("update_task", label="Uni")]}],
        },
    }

    calls = trace_calls(thread_trace, raw_input_id=RAW_1, raw_task_id=TASK)

    assert [(c.task_id, dict(c.tool_input)) for c in calls] == [
        (SUBTASK, {"existing_task_id": SUBTASK, "estimation": 20}),
        (TASK, {"label": "Uni"}),
    ]


def test_chat_calls_read_successful_task_tools_from_the_conversation():
    messages = [
        {"role": "user", "content": "add a task"},
        {"role": "assistant", "tools": [
            {"name": "tasks_search", "status": "success", "params": {"query": "x"}},
            {"name": "create_task", "status": "success", "params": {"estimation": 15},
             "artifact_refs": [f"task:{TASK}"]},
            {"name": "update_task", "status": "failed", "params": {"task_id": TASK}},
            {"name": "update_task", "status": "success",
             "params": {"task_id": TASK, "label": "Uni"}},
        ]},
    ]

    assert chat_calls(messages) == [
        AgentCall(TASK, {"estimation": 15}),
        AgentCall(TASK, {"task_id": TASK, "label": "Uni"}),
    ]
    assert chat_calls("not a list") == []


def _task(**overrides) -> dict:
    values = {
        "due_date": datetime(2026, 7, 2, 7, 0, tzinfo=timezone.utc),
        "estimation": 30,
        "label": "Uni",
        "work_type": None,
        "effort": None,
        "deadline_kind": "firm",
        "related_event_due_derived": False,
        "due_date_derived": False,
        "is_container": False,
    }
    values.update(overrides)
    return values


def test_last_agent_value_per_field_is_compared_with_the_current_value():
    calls = [
        AgentCall(
            TASK,
            {"estimation": 60, "label": "Admin", "due_date": "2026-07-01T09:00:00+02:00"},
            RAW_1,
        ),
        AgentCall(TASK, {"estimation": "45"}, RAW_2),
    ]

    rows = override_rows(_task(), calls)

    by_field = {row["field"]: row for row in rows}
    assert set(by_field) == {"estimation", "label", "due_date"}
    assert by_field["estimation"] == {
        "field": "estimation", "agent_value": 45, "user_value": 30, "raw_input_id": RAW_2,
    }
    assert by_field["label"]["agent_value"] == "Admin"
    assert by_field["label"]["raw_input_id"] == RAW_1
    assert by_field["due_date"]["agent_value"] == datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)


def test_due_dates_compare_as_instants_within_a_minute():
    current = _task(due_date=datetime(2026, 7, 2, 7, 0, 30, tzinfo=timezone.utc))
    same_instant_other_zone = AgentCall(TASK, {"due_date": "2026-07-02T09:00:00+02:00"})
    rounded_up = AgentCall(TASK, {"due_date": "2026-07-02T06:56:10Z"})
    naive_utc = AgentCall(TASK, {"due_date": "2026-07-02T07:00:00"})

    for call in (same_instant_other_zone, rounded_up, naive_utc):
        assert override_rows(current, [call]) == []


def test_attribute_fields_compare_only_valid_agent_values():
    calls = [AgentCall(
        TASK, {"work_type": "physical", "effort": "enormous", "deadline_kind": "soft"}
    )]

    rows = override_rows(_task(work_type="cognitive", deadline_kind="soft"), calls)

    assert [(r["field"], r["agent_value"], r["user_value"]) for r in rows] == [
        ("work_type", "physical", "cognitive"),
    ]


def test_values_the_app_derived_are_not_overrides():
    calls = [AgentCall(TASK, {"due_date": "2026-07-05T09:00:00Z", "estimation": 60})]

    assert override_rows(_task(related_event_due_derived=True, is_container=True), calls) == []
    assert [r["field"] for r in override_rows(_task(due_date_derived=True), calls)] == [
        "estimation"
    ]


def test_unparseable_or_missing_agent_values_are_ignored():
    calls = [AgentCall(TASK, {"due_date": "next friday", "estimation": None, "label": ""})]

    assert override_rows(_task(), calls) == []
