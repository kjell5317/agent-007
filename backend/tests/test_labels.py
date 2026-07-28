from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.tools.schemas import chat_tools, new_input_tools  # noqa: E402
from app.db.schemas.label import LabelUpdate  # noqa: E402
from app.services.calendar.labels import parse_labels  # noqa: E402

_CALENDAR = {
    "summary": "kjell@example.com",
    "labelProperties": {
        "eventLabels": [
            {"id": "0dd89748", "backgroundColor": "#009688", "name": "Coding"},
            {"id": "83E51814", "backgroundColor": "#000000", "name": "Reise"},
            # Incomplete entries can't be mirrored — no id to key on, or no
            # name for the agent and the task rows to reference.
            {"backgroundColor": "#ffffff", "name": "No id"},
            {"id": "77749834", "backgroundColor": "#7cb342"},
        ]
    },
}


def test_parse_labels_keeps_google_ids_verbatim_and_skips_incomplete():
    labels = parse_labels(_CALENDAR)

    assert [label.google_id for label in labels] == ["0dd89748", "83E51814"]
    assert labels[1].name == "Reise"
    assert labels[1].background_color == "#000000"


def test_parse_labels_tolerates_a_calendar_without_labels():
    assert parse_labels({"summary": "x"}) == []
    assert parse_labels({"labelProperties": {}}) == []


def test_labels_without_a_description_stay_out_of_the_agents_enum():
    # `agent_descriptions` only returns described labels; with none of them
    # described the catalog is empty and the field is absent entirely.
    create_task = next(t for t in new_input_tools({}) if t["name"] == "create_task")

    assert "label" not in create_task["parameters"]["properties"]
    assert "label" not in create_task["parameters"]["required"]


def test_label_enum_and_descriptions_reach_the_create_tool():
    catalog = {"Uni": "coursework, assignments and exams", "Arzt": "doctor visits"}
    tools = new_input_tools(catalog)
    create_task = next(t for t in tools if t["name"] == "create_task")
    label = create_task["parameters"]["properties"]["label"]

    assert label["enum"] == ["Uni", "Arzt"]
    assert "- Uni: coursework, assignments and exams" in label["description"]
    assert "label" in create_task["parameters"]["required"]
    # update_task takes a label too, but never demands one.
    update_task = next(t for t in tools if t["name"] == "update_task")
    assert "label" in update_task["parameters"]["properties"]
    assert "label" not in update_task["parameters"]["required"]


def test_chat_create_task_label_is_optional():
    create_task = next(t for t in chat_tools({"Uni": "coursework"}) if t["name"] == "create_task")

    assert "label" in create_task["parameters"]["properties"]
    assert "label" not in create_task["parameters"]["required"]


def test_building_tools_does_not_mutate_the_shared_schemas():
    # The chat runner appends its optional integration tools to what it gets
    # back, so a builder must never hand out the module-level list.
    first = new_input_tools({"Uni": "coursework"})
    first.append({"name": "extra"})

    assert [t["name"] for t in new_input_tools({})] != ["extra"]
    assert all(t["name"] != "extra" for t in new_input_tools({}))
    assert "label" not in next(
        t for t in new_input_tools({}) if t["name"] == "create_task"
    )["parameters"]["properties"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("kjell5317/agent-007", "kjell5317/agent-007"),
        ("  kjell5317/agent-007  ", "kjell5317/agent-007"),
        ("https://github.com/kjell5317/agent-007", "kjell5317/agent-007"),
        ("https://github.com/kjell5317/agent-007.git", "kjell5317/agent-007"),
        ("", None),
        (None, None),
    ],
)
def test_label_update_normalizes_the_repo(raw, expected):
    assert LabelUpdate(github_repo=raw).github_repo == expected


@pytest.mark.parametrize("raw", ["not-a-repo", "owner/", "/repo", "owner/repo/extra"])
def test_label_update_rejects_a_malformed_repo(raw):
    with pytest.raises(ValueError):
        LabelUpdate(github_repo=raw)
