from __future__ import annotations

import math
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers.jev import Answer  # noqa: E402
from app.agent.triage.features import LOGIT_CLIP, logit, p_task_choice, triage_features  # noqa: E402
from app.scripts import jev_replay  # noqa: E402


def _choice(p_task: float) -> Answer:
    return Answer(
        value="task" if p_task >= 0.5 else "not_task",
        p=max(p_task, 1 - p_task),
        probabilities={"task": p_task, "not_task": 1 - p_task},
    )


def _noul(p_yes: float) -> Answer:
    return Answer(value="yes" if p_yes >= 0.5 else "no", p=max(p_yes, 1 - p_yes),
                  probabilities={"yes": p_yes, "no": 1 - p_yes})  # fmt: skip


def test_logit_is_clipped():
    assert logit(0.5) == 0.0
    assert logit(1.0) == LOGIT_CLIP
    assert logit(0.0) == -LOGIT_CLIP


def test_p_task_choice_averages_both_orders():
    answers = {"is_task": _choice(0.8), "is_task_swapped": _choice(0.4)}
    assert p_task_choice(answers) == pytest.approx(0.6)
    assert p_task_choice({"is_task": _choice(0.3)}) == pytest.approx(0.3)
    assert p_task_choice({}) == 0.5


def test_triage_features_combine_jev_reads_with_history():
    answers = {
        "is_task": _choice(0.9), "is_task_swapped": _choice(0.7),
        "automated": _noul(0.95), "addressed_to_user": _noul(0.1),
    }  # fmt: skip
    candidates = [
        SimpleNamespace(status="not_task", similarity=0.82),
        SimpleNamespace(status="open", similarity=0.61),
        SimpleNamespace(status="not_task", similarity=0.7),
    ]
    features = triage_features(
        answers, source="slack", meta={"directed_at_me": False},
        sender_history={"not_task": 8, "open": 1, "duplicate": 1}, candidates=candidates,
    )  # fmt: skip

    assert features["is_task"] == pytest.approx(logit(0.8))
    assert features["automated"] == pytest.approx(logit(0.95))
    assert "fyi_only" not in features  # unasked questions stay out
    assert features["directed_at_me"] == -1.0
    assert features["slack"] == 1.0
    assert features["sender_task_share"] == pytest.approx(logit(3 / 12))
    assert features["sender_seen"] == pytest.approx(math.log1p(10))
    assert features["not_task_precedent"] == pytest.approx(0.82)
    assert features["task_precedent"] == pytest.approx(0.61)


def test_unknown_direction_and_new_sender_are_neutral():
    features = triage_features(
        {}, source="gmail", meta={}, sender_history={}, candidates=[]
    )
    assert features["directed_at_me"] == 0.0
    assert features["sender_task_share"] == 0.0
    assert features["sender_seen"] == 0.0


# --- replay helpers -------------------------------------------------------------------


def _item(source="gmail", truth="not_task", features=None, label=None, p_label=0.9, estimate="3"):
    task = SimpleNamespace(id=uuid.uuid4(), label=label, estimation=60, agent_estimation=None)
    raw = SimpleNamespace(
        id=uuid.uuid4(), source=source, status="open" if truth == "task" else truth,
        task_id=task.id if truth != "not_task" else None,
        received_at=datetime(2026, 10, 1, tzinfo=timezone.utc), source_metadata={}, content="",
    )  # fmt: skip
    answers = {
        "is_task": _choice(0.7 if truth != "not_task" else 0.3),
        "label": Answer(value=label or "none", p=p_label),
        "estimate": Answer(value=estimate, p=0.6),
    }
    return jev_replay.Replayed(
        raw=raw, truth=truth, agent=None, answers=answers, jev_trace={"answers": {}},
        features=features or {"is_task": 1.0 if truth != "not_task" else -1.0},
        task=task if truth != "not_task" else None,
    )  # fmt: skip


def test_truth_of_separates_follow_ups_and_dismissals():
    raw = SimpleNamespace(task_id=uuid.uuid4(), status="duplicate")
    assert jev_replay.truth_of(raw, "open") == "duplicate"
    assert jev_replay.truth_of(SimpleNamespace(task_id=raw.task_id, status="open"), "open") == "task"
    assert jev_replay.truth_of(SimpleNamespace(task_id=raw.task_id, status="open"), "not_task") == "not_task"
    assert jev_replay.truth_of(SimpleNamespace(task_id=None, status="not_task"), None) == "not_task"


def test_out_of_fold_scores_only_triage_inputs():
    items = [_item(truth="task" if i % 4 == 0 else "not_task") for i in range(60)]
    items.append(_item(source="manual", truth="task"))
    jev_replay.out_of_fold(items)

    assert all(i.p_model is not None for i in items if i.raw.source != "manual")
    assert items[-1].p_model is None
    task_p = [i.p_model for i in items[:-1] if i.truth == "task"]
    other_p = [i.p_model for i in items[:-1] if i.truth == "not_task"]
    assert min(task_p) > max(other_p)


def test_label_report_grades_only_catalog_labels(capsys):
    items = [
        _item(truth="task", label="Uni"),  # retired label: not gradable
        _item(truth="task", label="Sport"),
        _item(truth="task", label="Arzt", p_label=0.5),
    ]
    items[2].answers["label"] = Answer(value="Sport", p=0.5)
    jev_replay.label_report(items, {"Sport", "Arzt"})
    out = capsys.readouterr().out

    assert "label not in today's catalog or without description: 1" in out
    assert "p ≥ 0.0: 50.0% correct" in out
    assert "p ≥ 0.6: 100.0% correct, covers 50.0%" in out


def test_estimate_report_uses_the_agents_value_before_the_correction(capsys):
    item = _item(truth="task", estimate="3")  # Jev: 60 min
    corrected = {item.task.id: (120, 30)}  # user set 120, agent had said 30
    jev_replay.estimate_report([item], corrected, {})
    out = capsys.readouterr().out
    assert "your corrections" in out and "jev=1.00 (n=1)  agent=2.00 (n=1)" in out
