from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.services.learn import jev as learn_jev  # noqa: E402
from app.services.learn.jev import (  # noqa: E402
    DISABLED,
    Row,
    Thresholds,
    fit_threshold,
    fit_thresholds,
    rows_from_trace,
)


def test_fit_threshold_needs_enough_rows():
    assert fit_threshold([(0.99, True)] * 10, 0.9) is None


def test_fit_threshold_finds_the_lowest_threshold_that_meets_precision():
    rows = [(0.99, True)] * 40 + [(0.9, True)] * 10 + [(0.8, False)] * 10 + [(0.7, True)] * 10
    # ≥0.9: 50/50 correct; ≥0.8: 50/60; ≥0.7: 60/70.
    assert fit_threshold(rows, 0.95) == pytest.approx(0.9)
    # ≥0.8 dips below 0.86, so 0.9 is the lowest that holds; 0.85 is met again at 0.7.
    assert fit_threshold(rows, 0.86) == pytest.approx(0.9)
    assert fit_threshold(rows, 0.85) == pytest.approx(0.7)


def test_fit_threshold_disables_a_decision_that_never_meets_precision():
    rows = [(0.99, False)] * 30 + [(0.9, True)] * 30
    assert fit_threshold(rows, 0.98) == DISABLED


def test_fit_threshold_never_goes_below_the_floor():
    assert fit_threshold([(0.3, True)] * 60, 0.9) == learn_jev.FLOOR


def test_fit_thresholds_pools_all_sources():
    rows = [Row("gmail", "drop", 0.99, True)] * 30 + [Row("slack", "drop", 0.99, True)] * 30
    fitted = fit_thresholds(rows)
    assert "gmail" not in fitted  # 30 rows are not enough on their own
    assert fitted["all"]["drop"] == pytest.approx(0.99)


def _trace(p_task=None, **answers):
    trace = {"answers": answers, "candidates": {"A": str(TASK_ID)}}
    if p_task is not None:
        trace["p_task"] = p_task
    return trace


TASK_ID = uuid.uuid4()


def test_rows_from_trace_grades_a_dropped_task_as_wrong():
    rows = rows_from_trace("gmail", "open", TASK_ID, _trace(p_task=0.03), "open")
    drop = next(r for r in rows if r.decision == "drop")
    create = next(r for r in rows if r.decision == "create")
    assert drop.p == pytest.approx(0.97) and not drop.correct
    assert create.p == pytest.approx(0.03) and create.correct


def test_rows_from_trace_counts_a_dismissal_as_not_task():
    rows = rows_from_trace("gmail", "open", TASK_ID, _trace(p_task=0.9), "not_task")
    assert next(r for r in rows if r.decision == "drop").correct


def test_rows_from_trace_recomputes_p_task_with_fresh_weights():
    trace = {**_trace(p_task=0.5), "features": {"is_task": 2.0}}
    rows = rows_from_trace("gmail", "not_task", None, trace, None, {"is_task": 3.0, "bias": 0.0})
    drop = next(r for r in rows if r.decision == "drop")
    assert drop.p == pytest.approx(1 - learn_jev.predict({"is_task": 3.0}, {"is_task": 2.0}))
    assert drop.correct


def test_rows_from_trace_grades_the_matched_candidate():
    trace = _trace(
        p_task=0.9,
        target={"value": "A", "p": 0.96},
        target_action={"value": "nothing_new", "p": 0.93},
    )
    rows = {r.decision: r for r in rows_from_trace("gmail", "duplicate", TASK_ID, trace, "open")}
    assert rows["target"].correct and rows["no_change"].correct
    assert "create" not in rows

    wrong = {
        r.decision: r for r in rows_from_trace("gmail", "duplicate", uuid.uuid4(), trace, "open")
    }
    assert not wrong["target"].correct


def test_thresholds_and_model_belong_to_the_pinned_model_and_question_version(monkeypatch):
    rows = {
        learn_jev.JEV_THRESHOLDS: SimpleNamespace(
            params={
                "model": "jev-1.14.0",
                "questions": "2",
                "sources": {"all": {"drop": 0.95, "create": 0.85}, "gmail": {"drop": 0.99}},
            }
        ),
        learn_jev.TRIAGE_MODEL: SimpleNamespace(
            params={
                "model": "jev-1.14.0",
                "questions": "2",
                "weights": {"is_task": 1.5, "bias": -1.0},
            }
        ),
    }
    monkeypatch.setattr(learn_jev.models_store, "active", lambda session, kind: rows[kind])
    settings = SimpleNamespace(jev_model="jev-1.14.0")

    gmail = learn_jev.thresholds_for(None, "gmail", "2", settings)
    slack = learn_jev.thresholds_for(None, "slack", "2", settings)
    assert (gmail.drop, gmail.create) == (0.99, 0.85)
    assert slack.drop == 0.95
    assert learn_jev.triage_model_for(None, "2", settings) == {"is_task": 1.5, "bias": -1.0}

    assert learn_jev.thresholds_for(None, "gmail", "3", settings) == Thresholds()
    assert learn_jev.triage_model_for(None, "3", settings) is None
    newer = SimpleNamespace(jev_model="jev-1.15.0")
    assert learn_jev.thresholds_for(None, "gmail", "2", newer) == Thresholds()
    assert learn_jev.triage_model_for(None, "2", newer) is None


def test_fit_logistic_learns_the_signal_and_stays_calibrated():
    rows = []
    for i in range(400):
        signal = 1.0 if i % 2 else -1.0
        noise = (i % 7) / 7 - 0.5
        # The label follows the signal 90 % of the time, in both classes.
        label = int(signal < 0) if i % 20 in (0, 1) else int(signal > 0)
        rows.append(({"is_task": signal, "noise": noise}, label))
    weights = learn_jev.fit_logistic(rows)

    assert weights["is_task"] > 1.0
    assert abs(weights["noise"]) < 0.5
    assert learn_jev.predict(weights, {"is_task": 1.0, "noise": 0.0}) == pytest.approx(
        0.9, abs=0.03
    )
    assert learn_jev.predict(weights, {"is_task": -1.0, "noise": 0.0}) == pytest.approx(
        0.1, abs=0.03
    )


def test_refit_skips_the_model_without_enough_rows(monkeypatch):
    published = []
    monkeypatch.setattr(learn_jev.models_store, "active", lambda session, kind: None)
    monkeypatch.setattr(
        learn_jev.models_store, "publish", lambda session, **kw: published.append(kw)
    )
    raw = SimpleNamespace(task_id=None, status="not_task", source="gmail")
    answered = [(raw, {"features": {"is_task": -2.0}, "p_task": 0.1, "answers": {}}, None)] * 10
    settings = SimpleNamespace(jev_model="jev-1.14.0")

    assert (
        learn_jev.refit_triage_model(SimpleNamespace(commit=lambda: None), answered, "2", settings)
        is None
    )
    assert published == []
