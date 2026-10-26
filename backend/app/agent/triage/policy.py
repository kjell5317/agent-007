"""Which Jev answers are confident enough to act on without the LLM.

Thresholds are probabilities of the chosen option (not Jev's `confidence`,
whose scale depends on the option count); see `app.services.learn.jev`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.agent.helpers.jev import Answer
from app.services.learn.jev import Thresholds

DecisionKind = Literal["not_task", "no_change", "close", "create", "llm"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str
    target: str | None = None
    p: float = 0.0
    facts: bool = False


def yes(answer: Answer | None) -> float:
    return answer.p_of("yes") if answer is not None else 0.0


def decide_new_input(
    answers: Mapping[str, Answer], p_task: float, th: Thresholds, *, close_enabled: bool = False
) -> Decision:
    """`p_task` is the triage model's p(the input belongs to a task), or the
    both-orders Jev choice while no model is fitted."""
    facts = yes(answers.get("has_facts")) >= th.flag
    if yes(answers.get("has_event")) >= th.flag:
        return Decision("llm", "describes a calendar event", facts=facts)

    target = answers.get("target")
    if target is not None and (target.value != "none" or target.p < th.target):
        if target.p < th.target:
            return Decision(
                "llm", f"unsure about matching a candidate (p={target.p:.2f})", facts=facts
            )
        action = answers["target_action"]
        if action.value == "nothing_new" and action.p >= th.no_change:
            return Decision(
                "no_change", "adds nothing to the matched task", target.value, action.p, facts
            )
        if action.value == "done" and close_enabled and action.p >= th.close:
            return Decision(
                "close", "shows the matched task is done", target.value, action.p, facts
            )
        return Decision("llm", f"matched task needs {action.value}", target.value, action.p, facts)

    if 1.0 - p_task >= th.drop:
        return Decision("not_task", "not a task", p=1.0 - p_task, facts=facts)
    if p_task >= th.create:
        label = answers.get("label")
        if label is None or label.value == "none" or label.p < th.field:
            return Decision("llm", "task, but no confident label", p=p_task, facts=facts)
        return Decision("create", "a task for the user", p=p_task, facts=facts)
    return Decision("llm", f"unsure whether this is a task (p={p_task:.2f})", p=p_task, facts=facts)


def decide_followup(
    answers: Mapping[str, Answer], th: Thresholds, *, close_enabled: bool = False
) -> Decision:
    facts = yes(answers.get("has_facts")) >= th.flag
    if yes(answers.get("has_event")) >= th.flag:
        return Decision("llm", "describes a calendar event", facts=facts)
    action = answers["target_action"]
    if action.value == "nothing_new" and action.p >= th.no_change:
        return Decision("no_change", "adds nothing to the task", p=action.p, facts=facts)
    if action.value == "done" and close_enabled and action.p >= th.close:
        return Decision("close", "shows the task is done", p=action.p, facts=facts)
    return Decision(
        "llm", f"follow-up needs {action.value} (p={action.p:.2f})", p=action.p, facts=facts
    )
