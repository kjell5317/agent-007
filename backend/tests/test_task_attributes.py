from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import dispatch  # noqa: E402
from app.agent.helpers.attributes import new_task_attributes, valid_attributes  # noqa: E402
from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.input import runner as input_runner  # noqa: E402
from app.agent.manual import runner as manual_runner  # noqa: E402
from app.agent.tools.schemas import chat_tools, new_input_tools, thread_followup_tools  # noqa: E402


def _tool(tools: list[dict], name: str) -> dict:
    return next(t for t in tools if t["name"] == name)


def test_valid_attributes_keeps_known_values_and_drops_the_rest():
    assert valid_attributes(
        {"work_type": " Physical ", "effort": "extreme", "deadline_kind": "soft", "title": "x"}
    ) == {"work_type": "physical", "deadline_kind": "soft"}


def test_new_task_inherits_label_defaults_unless_the_agent_picked():
    defaults = {"Sport": ("physical", "medium"), "Uni": (None, "high")}

    assert new_task_attributes(
        {}, defaults, label="Sport", explicit_due=True, related_event=False
    ) == {"work_type": "physical", "effort": "medium", "deadline_kind": "firm"}
    assert new_task_attributes(
        {"effort": "low", "work_type": "nope"}, defaults,
        label="Sport", explicit_due=True, related_event=False,
    ) == {"work_type": "physical", "effort": "low", "deadline_kind": "firm"}
    assert new_task_attributes(
        {}, defaults, label="Uni", explicit_due=True, related_event=False
    )["work_type"] is None
    assert new_task_attributes(
        {}, defaults, label=None, explicit_due=True, related_event=False
    )["effort"] is None


@pytest.mark.parametrize(
    "explicit_due,related_event,expected",
    [(True, False, "firm"), (True, True, "firm"), (False, True, "hard"), (False, False, "soft")],
)
def test_deadline_kind_default(explicit_due, related_event, expected):
    attributes = new_task_attributes(
        {}, {}, label=None, explicit_due=explicit_due, related_event=related_event
    )
    assert attributes["deadline_kind"] == expected


def test_agent_deadline_kind_wins_over_the_default():
    attributes = new_task_attributes(
        {"deadline_kind": "hard"}, {}, label=None, explicit_due=False, related_event=False
    )
    assert attributes["deadline_kind"] == "hard"


@pytest.mark.parametrize("builder", [new_input_tools, chat_tools])
def test_task_tools_offer_optional_attribute_enums(builder):
    tools = builder({"Uni": "coursework"})
    for name in ("create_task", "update_task"):
        params = _tool(tools, name)["parameters"]
        assert params["properties"]["work_type"]["enum"] == ["physical", "cognitive", "social"]
        assert params["properties"]["effort"]["enum"] == ["low", "medium", "high"]
        assert params["properties"]["deadline_kind"]["enum"] == ["hard", "firm", "soft"]
        assert not {"work_type", "effort", "deadline_kind"} & set(params.get("required", []))
    create_props = _tool(tools, "create_task")["parameters"]["properties"]
    assert "label's default" in create_props["work_type"]["description"]
    assert "submissions, exams" in create_props["deadline_kind"]["description"]


def test_thread_update_task_offers_attribute_enums():
    props = _tool(thread_followup_tools({}), "update_task")["parameters"]["properties"]
    assert {"work_type", "effort", "deadline_kind"} <= set(props)


@pytest.mark.asyncio
async def test_update_task_passes_valid_attributes_and_drops_invalid(monkeypatch):
    patches = []

    async def fake_update_task_svc(_session, _task_id, patch):
        patches.append(patch)

    monkeypatch.setattr(dispatch, "update_task_svc", fake_update_task_svc)

    await dispatch.apply_task_action(
        SimpleNamespace(),
        SimpleNamespace(id=uuid.uuid4()),
        "update_task",
        {"effort": "HIGH", "work_type": "spiritual", "deadline_kind": "hard"},
    )

    assert patches == [{"effort": "high", "deadline_kind": "hard"}]


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


@pytest.mark.asyncio
async def test_new_input_create_task_fills_attributes_from_label_defaults(monkeypatch):
    created = []

    async def fake_chat(*_args, **_kwargs):
        return _response(ToolCall(id="c1", name="create_task", input={
            "title": "Go running",
            "estimation": 45,
            "due_date": "2026-07-02T09:00:00+00:00",
            "label": "Sport",
            "effort": "high",
        }))

    def fake_create(_session, payload):
        created.append(payload)
        return SimpleNamespace(id=uuid.uuid4(), title=payload.title)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(input_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(input_runner, "chat", fake_chat)
    monkeypatch.setattr(input_runner.tasks, "create", fake_create)
    monkeypatch.setattr(input_runner, "schedule_task", noop)
    monkeypatch.setattr(input_runner, "maybe_split_task", noop)
    monkeypatch.setattr(input_runner.raw_inputs, "finalize", lambda *_a, **_k: None)
    monkeypatch.setattr(
        input_runner.labels_store, "defaults_by_name", lambda _s: {"Sport": ("physical", "low")}
    )

    raw = SimpleNamespace(id=uuid.uuid4(), source="gmail", source_metadata={}, content="run")
    await input_runner.run_new_input_agent(SimpleNamespace(commit=lambda: None), raw, [], None)

    assert (created[0].work_type, created[0].effort, created[0].deadline_kind) == (
        "physical", "high", "soft",
    )


@pytest.mark.asyncio
async def test_manual_extraction_validates_and_defaults_attributes(monkeypatch):
    async def fake_chat(*_args, **_kwargs):
        return _response(ToolCall(id="c1", name="create_task", input={
            "title": "Submit thesis",
            "estimation": 60,
            "due_date": "2026-07-02T09:00:00+00:00",
            "due_date_is_explicit": True,
            "label": "Uni",
            "effort": "huge",
        }))

    monkeypatch.setattr(manual_runner, "get_settings", lambda: SimpleNamespace(user_timezone="UTC"))
    monkeypatch.setattr(manual_runner, "chat", fake_chat)
    monkeypatch.setattr(
        manual_runner.labels_store, "defaults_by_name", lambda _s: {"Uni": ("cognitive", None)}
    )
    raw = SimpleNamespace(id="raw-1", source="gmail", source_metadata={}, content="thesis")

    payload = await manual_runner.extract_task_fields(SimpleNamespace(), raw)

    assert payload["work_type"] == "cognitive"
    assert payload["deadline_kind"] == "firm"
    assert "effort" not in payload
    assert "due_date_is_explicit" not in payload
