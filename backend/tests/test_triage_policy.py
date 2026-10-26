from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers.jev import Answer  # noqa: E402
from app.agent.triage import policy  # noqa: E402
from app.agent.triage.fields import draft_fields  # noqa: E402
from app.agent.triage.state import (  # noqa: E402
    StateRefs,
    candidate_title,
    clean_subject,
    followup_state,
    new_input_state,
)
from app.services.learn.jev import Thresholds  # noqa: E402

TH = Thresholds()
TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 21, 15, 10, tzinfo=TZ)


def _a(value: str, p: float = 0.99) -> Answer:
    return Answer(value=value, p=p, probabilities={value: p})


def _noul(p_yes: float) -> Answer:
    value = "yes" if p_yes >= 0.5 else "no"
    return Answer(
        value=value, p=max(p_yes, 1 - p_yes), probabilities={"yes": p_yes, "no": 1 - p_yes}
    )


def _answers(**overrides) -> dict[str, Answer]:
    answers = {
        "has_event": _noul(0.05),
        "has_facts": _noul(0.05),
        "label": _a("Uni", 0.9),
        "work_type": _a("cognitive", 0.8),
        "effort": _a("medium", 0.5),
        "estimate": _a("1", 0.6),
        "urgency": _a("this_week", 0.7),
        "deadline_stakes": _a("hard", 0.8),
        "at_home": _noul(0.1),
        "names_place": _noul(0.1),
        "title_ok": _noul(0.95),
        "needs_description": _noul(0.2),
        "split": _noul(0.1),
        "due_mode": _a("none", 0.9),
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


# --- policy -------------------------------------------------------------------


def test_confident_not_task_is_dropped():
    decision = policy.decide_new_input(_answers(), 0.01, TH)
    assert decision.kind == "not_task"
    assert not decision.facts


def test_not_task_below_the_drop_threshold_goes_to_the_llm():
    assert policy.decide_new_input(_answers(), 0.1, TH).kind == "llm"


def test_event_always_goes_to_the_llm():
    answers = _answers(has_event=_noul(0.6))
    assert policy.decide_new_input(answers, 0.001, TH).kind == "llm"


def test_facts_ride_along_without_blocking_the_decision():
    decision = policy.decide_new_input(_answers(has_facts=_noul(0.8)), 0.01, TH)
    assert decision.kind == "not_task" and decision.facts


def test_matched_candidate_with_nothing_new_is_no_change():
    answers = _answers(target=_a("B", 0.95), target_action=_a("nothing_new", 0.95))
    decision = policy.decide_new_input(answers, 0.99, TH)
    assert (decision.kind, decision.target) == ("no_change", "B")


def test_matched_candidate_with_changes_goes_to_the_llm():
    answers = _answers(target=_a("A", 0.95), target_action=_a("changes", 0.95))
    assert policy.decide_new_input(answers, 0.99, TH).kind == "llm"


def test_close_needs_the_flag():
    answers = _answers(target=_a("A", 0.99), target_action=_a("done", 0.99))
    assert policy.decide_new_input(answers, 0.99, TH).kind == "llm"
    assert policy.decide_new_input(answers, 0.99, TH, close_enabled=True).kind == "close"


def test_unsure_none_target_goes_to_the_llm():
    answers = _answers(target=_a("none", 0.6), target_action=_a("nothing_new"))
    assert policy.decide_new_input(answers, 0.99, TH).kind == "llm"


def test_confident_none_target_falls_through_to_create():
    answers = _answers(target=_a("none", 0.95), target_action=_a("nothing_new"))
    assert policy.decide_new_input(answers, 0.99, TH).kind == "create"


def test_create_needs_a_confident_label():
    assert policy.decide_new_input(_answers(label=_a("none", 0.9)), 0.99, TH).kind == "llm"
    assert policy.decide_new_input(_answers(label=_a("Uni", 0.4)), 0.99, TH).kind == "llm"


def test_followup_decisions():
    assert (
        policy.decide_followup(
            {"target_action": _a("nothing_new"), "has_event": _noul(0.0), "has_facts": _noul(0.0)},
            TH,
        ).kind
        == "no_change"
    )
    assert (
        policy.decide_followup(
            {"target_action": _a("changes"), "has_event": _noul(0.0), "has_facts": _noul(0.0)}, TH
        ).kind
        == "llm"
    )
    assert (
        policy.decide_followup(
            {"target_action": _a("done"), "has_event": _noul(0.0), "has_facts": _noul(0.0)},
            TH,
            close_enabled=True,
        ).kind
        == "close"
    )


def test_thresholds_take_known_overrides_only():
    th = TH.with_overrides({"drop": 0.99, "bogus": 1})
    assert th.drop == 0.99 and th.create == TH.create


# --- fields -------------------------------------------------------------------


def test_fields_without_a_stated_deadline_need_no_writer():
    draft = draft_fields(_answers(), StateRefs(title="Submit thesis draft"), now=NOW, th=TH)

    assert draft.writer_needs() == set()
    assert draft.label == "Uni"
    assert draft.effort is None  # p below the field threshold: the label default applies
    assert draft.estimation == 30
    assert draft.due_date == datetime(2026, 10, 23, 23, 45, tzinfo=TZ)
    assert draft.due_date_is_explicit is False
    assert draft.deadline_kind is None
    assert draft.task_fields()["label"] == "Uni"


def test_stated_deadline_is_assembled_and_keeps_its_stakes():
    answers = _answers(
        due_mode=_a("relative"),
        due_anchor=_a("tomorrow"),
        due_hour=_a("09"),
    )
    draft = draft_fields(answers, StateRefs(title="Pay rent"), now=NOW, th=TH)

    assert draft.due_date == datetime(2026, 10, 22, 9, 0, tzinfo=TZ)
    assert draft.due_date_is_explicit is True
    assert draft.deadline_kind == "hard"
    assert "due_date" not in draft.writer_needs()


def test_unreadable_deadline_goes_to_the_writer():
    answers = _answers(due_mode=_a("relative", 0.5), due_anchor=_a("weekday", 0.5))
    draft = draft_fields(answers, StateRefs(title="Pay rent"), now=NOW, th=TH)
    assert draft.due_date is None
    assert "due_date" in draft.writer_needs()


def test_writer_needs_follow_the_text_questions():
    answers = _answers(
        title_ok=_noul(0.4),
        needs_description=_noul(0.8),
        names_place=_noul(0.9),
    )
    draft = draft_fields(answers, StateRefs(title="Re: stuff"), now=NOW, th=TH)
    assert draft.writer_needs() == {"title", "description", "location"}


def test_no_candidate_title_always_needs_a_title():
    draft = draft_fields(_answers(), StateRefs(title=None), now=NOW, th=TH)
    assert "title" in draft.writer_needs()


def test_home_event_and_link_map_back_through_refs():
    refs = StateRefs(
        title="Clean the kitchen",
        events={"E1": ("ev-1", "cal-1")},
        urls={"U1": "https://x.test"},
    )
    answers = _answers(at_home=_noul(0.9), related_event=_a("E1", 0.8), link=_a("U1", 0.9))
    fields = draft_fields(answers, refs, now=NOW, th=TH).task_fields()
    assert fields["location"] == "home"
    assert (fields["related_event_id"], fields["related_event_calendar_id"]) == ("ev-1", "cal-1")
    assert fields["link"] == "https://x.test"


# --- state --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "clean"),
    [
        ("Re: AW: [team] Fwd: Budget 2027", "Budget 2027"),
        ("WG: Termin", "Termin"),
        ("Re[2]: Hello", "Hello"),
        ("Plain subject", "Plain subject"),
        (None, ""),
    ],
)
def test_clean_subject(subject, clean):
    assert clean_subject(subject) == clean


def test_candidate_title_prefers_user_then_subject_then_first_line():
    raw = SimpleNamespace(
        source="gmail", content="Body line\nmore", source_metadata={"subject": "Re: X"}
    )
    assert candidate_title(raw, "  Typed  ") == "Typed"
    assert candidate_title(raw) == "X"
    slack = SimpleNamespace(
        source="slack", content="\n  Can you check the deploy?\nthanks", source_metadata={}
    )
    assert candidate_title(slack) == "Can you check the deploy?"


def test_new_input_state_maps_candidates_to_neutral_ids():
    task_id = uuid.uuid4()
    hit = SimpleNamespace(
        status="open",
        similarity=0.8123,
        sender="a@b.c",
        received_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    task = SimpleNamespace(
        id=task_id, title="Send report", due_date=datetime(2026, 10, 30, tzinfo=timezone.utc)
    )
    event = SimpleNamespace(event_id="ev", calendar_id="cal", summary="Trip", starts_at=None)
    raw = SimpleNamespace(
        source="gmail",
        content="x" * 5000,
        source_metadata={
            "from": "a@b.c",
            "subject": "Report",
            "directed_at_me": True,
            "urls": ["https://u.test/1"],
        },
    )

    state, refs = new_input_state(
        raw,
        now=NOW,
        task_candidates=[(hit, task)],
        related_events=[event],
    )

    assert refs.candidates == {"A": task_id}
    assert refs.events == {"E1": ("ev", "cal")}
    assert refs.urls == {"U1": "https://u.test/1"}
    assert state["candidates"][0] == {
        "id": "A",
        "status": "open",
        "title": "Send report",
        "similarity": 0.81,
        "due": "2026-10-30",
        "from": "a@b.c",
        "received": "2026-10-01",
    }
    # History is the triage model's job, not Jev's.
    assert "not_task_precedents" not in state and "sender_history" not in state
    assert state["input"]["directed_at_me"] is True
    assert state["input"]["body"].endswith(" […]")
    assert state["candidate_title"] == "Report"
    assert state["weekday"] == "Wednesday"


def test_followup_state_carries_the_task():
    task = SimpleNamespace(title="Send report", description="  For Q3  ", due_date=None)
    raw = SimpleNamespace(source="gmail", content="Thanks!", source_metadata={})
    state = followup_state(raw, task, now=NOW, status="open")
    assert state["task"] == {"title": "Send report", "status": "open", "description": "For Q3"}
    assert state["input"]["body"] == "Thanks!"
