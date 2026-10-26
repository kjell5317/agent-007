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


def _trace(**answers):
    return {"answers": answers, "candidates": {"A": str(TASK_ID)}}


TASK_ID = uuid.uuid4()


def test_rows_from_trace_grades_a_dropped_task_as_wrong():
    rows = rows_from_trace(
        "gmail", "open", TASK_ID, _trace(is_task={"value": "not_task", "p": 0.97}), "open"
    )
    drop = next(r for r in rows if r.decision == "drop")
    create = next(r for r in rows if r.decision == "create")
    assert (drop.p, drop.correct) == (0.97, False)
    assert create.p == pytest.approx(0.03) and create.correct


def test_rows_from_trace_counts_a_dismissal_as_not_task():
    rows = rows_from_trace(
        "gmail", "open", TASK_ID, _trace(is_task={"value": "task", "p": 0.9}), "not_task"
    )
    assert next(r for r in rows if r.decision == "drop").correct


def test_rows_from_trace_grades_the_matched_candidate():
    trace = _trace(
        is_task={"value": "task", "p": 0.9},
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


def test_thresholds_for_uses_the_fitted_values_of_the_pinned_model(monkeypatch):
    row = SimpleNamespace(
        params={
            "model": "jev-1.14.0",
            "sources": {"all": {"drop": 0.95, "create": 0.85}, "gmail": {"drop": 0.99}},
        }
    )
    monkeypatch.setattr(learn_jev.models_store, "active", lambda session, kind: row)
    settings = SimpleNamespace(jev_model="jev-1.14.0")

    gmail = learn_jev.thresholds_for(None, "gmail", settings)
    slack = learn_jev.thresholds_for(None, "slack", settings)
    assert (gmail.drop, gmail.create) == (0.99, 0.85)
    assert slack.drop == 0.95

    other = learn_jev.thresholds_for(None, "gmail", SimpleNamespace(jev_model="jev-1.15.0"))
    assert other == Thresholds()
