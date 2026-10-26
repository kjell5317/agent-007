"""Triage model features: Jev's narrow reads plus the user's own history.

Jev's probabilities go in as log-odds so a confident read can outweigh a
lukewarm one; history comes from what earlier inputs turned into. The model
fitted on these (`app.services.learn.jev`) is what adapts triage to this user
— Jev itself serves the same weights to every account.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from app.agent.helpers.jev import Answer
from app.agent.triage.questions import SIGNAL_QUESTIONS

LOGIT_CLIP = 6.0
_LINKED_STATUSES = ("open", "closed", "duplicate")


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return max(-LOGIT_CLIP, min(LOGIT_CLIP, math.log(p / (1 - p))))


def p_task_choice(answers: Mapping[str, Answer]) -> float:
    """p(task) averaged over both option orders, cancelling Jev's first-option lean."""
    reads = [
        answers[name].p_of("task") for name in ("is_task", "is_task_swapped") if name in answers
    ]
    return sum(reads) / len(reads) if reads else 0.5


def triage_features(
    answers: Mapping[str, Answer],
    *,
    source: str,
    meta: Mapping[str, Any],
    sender_history: Mapping[str, int],
    candidates: Sequence[Any],
) -> dict[str, float]:
    features = {"is_task": logit(p_task_choice(answers))}
    for name in SIGNAL_QUESTIONS:
        if name in answers:
            features[name] = logit(answers[name].p_of("yes"))
    directed = meta.get("directed_at_me")
    features["directed_at_me"] = 0.0 if directed is None else (1.0 if directed else -1.0)
    features["slack"] = 1.0 if source == "slack" else 0.0
    total = sum(sender_history.values())
    linked = sum(sender_history.get(status, 0) for status in _LINKED_STATUSES)
    # Smoothed toward an even split so a sender with one past input isn't decisive.
    features["sender_task_share"] = logit((linked + 1) / (total + 2))
    features["sender_seen"] = math.log1p(total)
    features["not_task_precedent"] = max(
        (float(c.similarity) for c in candidates if c.status == "not_task"), default=0.0
    )
    features["task_precedent"] = max(
        (float(c.similarity) for c in candidates if c.status in ("open", "closed")), default=0.0
    )
    return features
