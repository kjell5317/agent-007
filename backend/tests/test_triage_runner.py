from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import jev  # noqa: E402
from app.agent.helpers.jev import Answer, JevResult  # noqa: E402
from app.agent.helpers.llm import LLMMessage, LLMResponse, ToolCall  # noqa: E402
from app.agent.triage import runner, write  # noqa: E402
from app.db.clients.raw_inputs import SimilarInput  # noqa: E402
from app.services.learn.jev import Thresholds  # noqa: E402

ACTING = SimpleNamespace(
    typesafe_api_key="ts-key",
    jev_mode="on",
    jev_close_enabled=False,
    jev_model="jev-1.14.0",
    user_timezone="Europe/Berlin",
)
SHADOW = SimpleNamespace(**{**vars(ACTING), "jev_mode": "shadow"})


def _a(value: str, p: float = 0.99) -> Answer:
    return Answer(value=value, p=p, probabilities={value: p})


def _noul(p_yes: float) -> Answer:
    value = "yes" if p_yes >= 0.5 else "no"
    return Answer(
        value=value, p=max(p_yes, 1 - p_yes), probabilities={"yes": p_yes, "no": 1 - p_yes}
    )


def _answers(**overrides) -> dict[str, Answer]:
    answers = {
        "is_task": _a("task"),
        "has_event": _noul(0.02),
        "has_facts": _noul(0.02),
        "label": _a("Uni", 0.9),
        "work_type": _a("cognitive", 0.9),
        "effort": _a("medium", 0.9),
        "estimate": _a("3", 0.7),
        "urgency": _a("tomorrow", 0.8),
        "deadline_stakes": _a("firm", 0.8),
        "at_home": _noul(0.1),
        "names_place": _noul(0.1),
        "title_ok": _noul(0.97),
        "needs_description": _noul(0.1),
        "split": _noul(0.1),
        "due_mode": _a("none", 0.95),
        "due_month": _a("none"),
        "due_day": _a("none"),
        "due_year": _a("none"),
        "due_anchor": _a("none"),
        "due_weekday": _a("none"),
        "due_week": _a("none"),
        "due_hour": _a("none"),
        "due_minute": _a("00"),
    }
    answers.update(overrides)
    return answers


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture
def wired(monkeypatch):
    """Stub every I/O edge of the runner; tests set `state["answers"]`."""
    state: dict = {
        "answers": _answers(),
        "settings": ACTING,
        "asked": [],
        "finalized": [],
        "created": [],
        "actions": [],
        "written": [],
        "scheduled": [],
    }

    async def ask(st, questions, settings, *, name="jev"):
        state["asked"].append((name, set(questions)))
        answers = {k: v for k, v in state["answers"].items() if k in questions}
        return JevResult(model="jev-1.14.0", answers=answers, input_tokens=100, latency_ms=50)

    def finalize(session, raw_id, *, status, task_id=None, agent_trace=None):
        state["finalized"].append({"status": status, "task_id": task_id, "trace": agent_trace})

    def create(session, payload):
        state["created"].append(payload)
        return SimpleNamespace(id=uuid.uuid4(), title=payload.title)

    async def apply_task_action(session, task, tool, tool_input):
        state["actions"].append((task.id, tool, tool_input))
        return {"outcome": "no_change" if tool == "no_change" else "closed"}

    async def write_fields(raw, needs, settings, *, examples=()):
        state["written"].append(set(needs))
        return {"title": "Written title"} if "title" in needs else {}

    async def schedule(session, task):
        state["scheduled"].append(task.id)

    async def no_split(session, task, *, split_hint=None):
        return []

    monkeypatch.setattr(runner, "get_settings", lambda: state["settings"])
    monkeypatch.setattr(runner.jev, "ask", ask)
    monkeypatch.setattr(
        runner, "thresholds_for", lambda session, source, settings=None: Thresholds()
    )
    monkeypatch.setattr(runner.raw_inputs, "finalize", finalize)
    monkeypatch.setattr(runner.raw_inputs, "sender_history", lambda *a, **k: {})
    monkeypatch.setattr(runner.tasks_store, "create", create)
    monkeypatch.setattr(
        runner.tasks_store,
        "get",
        lambda session, task_id: SimpleNamespace(
            id=task_id, title="Existing task", due_date=None, is_container=False
        ),
    )
    monkeypatch.setattr(runner.tasks_store, "recent_titles", lambda session, label, limit=3: [])
    monkeypatch.setattr(runner.tasks_store, "latest_status_for", lambda session, ids: {})
    monkeypatch.setattr(runner, "apply_task_action", apply_task_action)
    monkeypatch.setattr(
        runner, "apply_event_context", lambda session, fields, explicit_due=False: (fields, None)
    )
    monkeypatch.setattr(runner, "write_fields", write_fields)
    monkeypatch.setattr(runner, "schedule_task", schedule)
    monkeypatch.setattr(runner, "maybe_split_task", no_split)
    monkeypatch.setattr(
        runner.labels_store, "agent_descriptions", lambda session: {"Uni": "University work"}
    )
    return state


def _raw(**overrides):
    values = {
        "id": uuid.uuid4(),
        "source": "gmail",
        "content": "Please submit the form.",
        "source_metadata": {"from": "Prof <prof@uni.test>", "subject": "Submit the enrolment form"},
        "embedding": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _hit(task_id, status="open") -> SimilarInput:
    return SimilarInput(
        id=uuid.uuid4(),
        source="gmail",
        status=status,
        task_id=task_id,
        similarity=0.8,
        decayed_similarity=0.8,
        agent_trace={},
        subject="Old",
        sender="prof@uni.test",
        content_snippet="",
        received_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_confident_not_task_is_finalized_without_an_llm_call(wired):
    wired["answers"] = _answers(is_task=_a("not_task", 0.995))
    outcome = await runner.new_input(FakeSession(), _raw(), [], [])

    assert outcome.trace["outcome"] == "not_task"
    assert outcome.trace["branch"] == "jev_not_task"
    assert wired["finalized"][0]["status"] == "not_task"
    assert outcome.trace["jev"]["decision"] == "not_task"
    assert wired["written"] == []


@pytest.mark.asyncio
async def test_shadow_mode_only_reports_answers(wired):
    wired["settings"] = SHADOW
    wired["answers"] = _answers(is_task=_a("not_task", 0.995))
    outcome = await runner.new_input(FakeSession(), _raw(), [], [])

    assert outcome.trace is None
    assert outcome.jev["decision"] == "not_task"
    assert wired["finalized"] == []


@pytest.mark.asyncio
async def test_jev_down_hands_over_to_the_agent(wired, monkeypatch):
    async def down(*args, **kwargs):
        raise jev.JevUnavailable("HTTP 529")

    monkeypatch.setattr(runner.jev, "ask", down)
    outcome = await runner.new_input(FakeSession(), _raw(), [], [])
    assert outcome.trace is None and "error" in outcome.jev


@pytest.mark.asyncio
async def test_duplicate_of_a_candidate_is_linked_with_no_change(wired):
    task_id = uuid.uuid4()
    wired["answers"] = _answers(target=_a("A", 0.97), target_action=_a("nothing_new", 0.97))
    outcome = await runner.new_input(FakeSession(), _raw(), [_hit(task_id)], [])

    assert wired["actions"][0][:2] == (task_id, "no_change")
    assert wired["finalized"][0]["status"] == "duplicate"
    assert wired["finalized"][0]["task_id"] == task_id
    assert outcome.trace["existing_task_id"] == str(task_id)
    name, asked = wired["asked"][0]
    assert name == "jev-triage"
    assert {"target", "target_action"} <= asked


@pytest.mark.asyncio
async def test_create_uses_jev_fields_and_skips_the_writer_when_nothing_is_missing(wired):
    outcome = await runner.new_input(FakeSession(), _raw(), [], [])

    payload = wired["created"][0]
    assert payload.title == "Submit the enrolment form"
    assert payload.label == "Uni"
    assert payload.estimation == 60  # level 3 → 60 minutes, no learned correction fitted
    assert payload.estimation_source == "jev"
    assert payload.deadline_kind == "soft"
    assert wired["written"] == [set()]
    assert outcome.trace["outcome"] == "task_created"
    assert wired["finalized"][0]["status"] == "open"
    assert wired["scheduled"]


@pytest.mark.asyncio
async def test_create_asks_the_writer_for_a_title_when_the_subject_is_poor(wired):
    wired["answers"] = _answers(title_ok=_noul(0.2))
    await runner.new_input(FakeSession(), _raw(), [], [])

    assert wired["written"] == [{"title"}]
    assert wired["created"][0].title == "Written title"


@pytest.mark.asyncio
async def test_writer_failure_hands_over_to_the_agent(wired, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(runner, "write_fields", broken)
    wired["answers"] = _answers(title_ok=_noul(0.2))
    outcome = await runner.new_input(FakeSession(), _raw(), [], [])
    assert outcome.trace is None and wired["created"] == []


@pytest.mark.asyncio
async def test_followup_with_nothing_new_is_no_change(wired):
    wired["answers"] = {
        "target_action": _a("nothing_new", 0.95),
        "has_event": _noul(0.0),
        "has_facts": _noul(0.0),
    }
    task = SimpleNamespace(id=uuid.uuid4(), title="Send report", description=None, due_date=None)
    outcome = await runner.followup(FakeSession(), _raw(content="Thanks!"), task)

    assert outcome.trace["branch"] == "jev_followup"
    assert wired["actions"][0][1] == "no_change"
    assert wired["finalized"][0]["status"] == "duplicate"


@pytest.mark.asyncio
async def test_manual_fields_need_no_llm_for_a_clean_typed_task(wired, monkeypatch):
    async def no_events(session, raw):
        return []

    monkeypatch.setattr(runner, "_related_events", no_events)
    raw = _raw(source="manual", content="Buy printer paper", source_metadata={"manual": True})
    outcome = await runner.manual(FakeSession(), raw, {})

    assert outcome.fields["title"] == "Buy printer paper"
    assert outcome.fields["label"] == "Uni"
    assert outcome.fields["estimation"] == 60
    assert outcome.fields["due_date"] is not None
    assert wired["written"] == [set()]
    assert outcome.trace["agent"] == "jev"


@pytest.mark.asyncio
async def test_manual_keeps_the_user_estimate_and_title(wired, monkeypatch):
    async def no_events(session, raw):
        return []

    monkeypatch.setattr(runner, "_related_events", no_events)
    wired["answers"] = _answers(title_ok=_noul(0.1))
    raw = _raw(source="manual", content="paper", source_metadata={"manual": True})
    outcome = await runner.manual(FakeSession(), raw, {"title": "Buy paper", "estimation": 20})

    assert "estimation" not in outcome.fields and "estimation_source" not in outcome.fields
    assert wired["written"] == [set()]  # the user's title is never rewritten


@pytest.mark.asyncio
async def test_manual_without_a_confident_label_runs_the_extractor(wired, monkeypatch):
    async def no_events(session, raw):
        return []

    monkeypatch.setattr(runner, "_related_events", no_events)
    wired["answers"] = _answers(label=_a("none", 0.9))
    outcome = await runner.manual(FakeSession(), _raw(source="manual"), {})
    assert outcome.fields is None
    assert outcome.trace["jev"]["why"] == "no confident label"


@pytest.mark.asyncio
async def test_manual_matches_related_events_in_a_second_call(wired, monkeypatch):
    event = SimpleNamespace(event_id="ev", calendar_id="cal", summary="Lisbon trip", starts_at=None)

    async def events(session, raw):
        return [event]

    monkeypatch.setattr(runner, "_related_events", events)
    wired["answers"] = _answers(related_event=_a("E1", 0.9))
    raw = _raw(source="manual", content="Book hotel", source_metadata={"manual": True})
    outcome = await runner.manual(FakeSession(), raw, {})

    assert [name for name, _ in wired["asked"]] == ["jev-manual", "jev-manual-event"]
    assert outcome.fields["related_event_id"] == "ev"


def _response(tool_input: dict) -> LLMResponse:
    call = ToolCall(id="w1", name=write.TOOL_NAME, input=tool_input)
    return LLMResponse(
        message=LLMMessage(role="assistant", tool_calls=(call,)),
        tool_calls=(call,),
        text="",
        stop_reason="tool_use",
        usage={},
        meta={},
        provider="test",
        model="test",
    )


def test_write_tool_carries_only_the_missing_fields():
    tool = write.write_tool({"title", "due_date"})
    assert set(tool["parameters"]["properties"]) == {"title", "due_date"}
    assert set(tool["parameters"]["required"]) == {"title", "due_date"}


@pytest.mark.asyncio
async def test_write_fields_parses_and_rounds_the_due_date(monkeypatch):
    seen = []

    async def fake_chat(messages, settings, **kwargs):
        seen.append(kwargs)
        return _response({"title": " Pay rent ", "due_date": "2026-10-30T09:05:00+01:00"})

    monkeypatch.setattr(write, "chat", fake_chat)
    raw = _raw(content="Rent is due Friday 9am")
    out = await write.write_fields(raw, {"title", "due_date"}, ACTING, examples=["Send Q3 report"])

    assert out["title"] == "Pay rent"
    assert out["due_date"].minute == 15
    assert seen[0]["force_tool"] == write.TOOL_NAME


@pytest.mark.asyncio
async def test_write_fields_makes_no_call_when_nothing_is_missing(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("no call expected")

    monkeypatch.setattr(write, "chat", never)
    assert await write.write_fields(_raw(), set(), ACTING) == {}
