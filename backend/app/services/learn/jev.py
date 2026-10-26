"""What triage learns from how inputs actually ended: the triage model and
the per-source thresholds, both refit nightly.

Every input Jev answered (shadow or acting) is a labelled example once its
final status is known — dismissals and promotions included, since they
rewrite the status.

- **Triage model.** A logistic regression over the features in
  `agent/triage/features.py` (Jev's narrow reads plus sender history and
  precedents), giving p(the input ends up linked to a task). It is the part
  that adapts to this user: Jev itself serves one set of weights to everyone.
- **Thresholds.** For each decision, the lowest probability whose answers at
  or above it reach the decision's precision target. Without enough rows the
  conservative defaults in `Thresholds` stay; a decision whose answers never
  reach the target is switched off.

Both are keyed by the Jev model and the triage questions' version: new
wording or a new model starts from scratch.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.clients import learned_models as models_store
from app.db.clients import raw_inputs, tasks as tasks_store

log = logging.getLogger(__name__)

JEV_THRESHOLDS = "jev_thresholds"
TRIAGE_MODEL = "jev_triage_model"
WINDOW = timedelta(days=180)
PRECISION = {"drop": 0.98, "create": 0.9, "target": 0.95, "no_change": 0.95}
MIN_ROWS = 50
MIN_SUPPORT = 10
FLOOR = 0.5
DISABLED = 1.01
MODEL_MIN_ROWS = 200
MODEL_MIN_POSITIVES = 20
# Gaussian prior on the weights (L2): shrinks toward "feature doesn't matter"
# so a few hundred rows can't overfit. The bias is left nearly free.
WEIGHT_SIGMA = 2.0
BIAS_SIGMA = 10.0


@dataclass(frozen=True)
class Thresholds:
    """Probabilities of the chosen option (not Jev's `confidence`, whose scale
    depends on the option count) above which triage acts without the LLM."""

    # A dropped input is the expensive mistake: a task the user never sees.
    drop: float = 0.97
    create: float = 0.9
    target: float = 0.9
    no_change: float = 0.9
    close: float = 0.97
    # Nouls above this send the input to the LLM (event) or a notes call (facts).
    flag: float = 0.3
    field: float = 0.6
    title_ok: float = 0.9
    date: float = 0.8

    def with_overrides(self, values: Mapping[str, Any]) -> Thresholds:
        known = {f.name for f in fields(self)}
        return replace(self, **{k: float(v) for k, v in values.items() if k in known})

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class Row:
    source: str
    decision: str
    p: float
    correct: bool


# --- Triage model ---------------------------------------------------------------


def fit_logistic(rows: Sequence[tuple[Mapping[str, float], int]], *, max_iter: int = 50) -> dict:
    """MAP logistic regression (Newton steps). Returns `{feature: weight}`
    plus `bias`."""
    names = sorted({name for x, _ in rows for name in x})
    X = np.zeros((len(rows), len(names) + 1))
    X[:, -1] = 1.0
    index = {name: n for n, name in enumerate(names)}
    for r, (x, _) in enumerate(rows):
        for name, value in x.items():
            X[r, index[name]] = value
    y = np.array([float(label) for _, label in rows])
    precision = np.diag([1 / WEIGHT_SIGMA**2] * len(names) + [1 / BIAS_SIGMA**2])
    w = np.zeros(len(names) + 1)
    for _ in range(max_iter):
        p = 0.5 * (1.0 + np.tanh(0.5 * (X @ w)))
        gradient = X.T @ (y - p) - precision @ w
        hessian = (X.T * (p * (1 - p))) @ X + precision
        step = np.linalg.solve(hessian, gradient)
        w += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return {**{name: float(w[n]) for name, n in index.items()}, "bias": float(w[-1])}


def predict(weights: Mapping[str, float], features: Mapping[str, float]) -> float:
    z = weights.get("bias", 0.0) + sum(weights.get(k, 0.0) * v for k, v in features.items())
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def triage_model_for(
    session: Session, version: str, settings: Settings | None = None
) -> dict[str, float] | None:
    settings = settings or get_settings()
    row = models_store.active(session, TRIAGE_MODEL)
    params = (row.params or {}) if row is not None else {}
    if params.get("model") != settings.jev_model or params.get("questions") != version:
        return None
    return dict(params.get("weights") or {}) or None


def linked_to_task(task_id, task_status: str | None) -> bool:
    """The label: the input ended up attached to a task (new or existing)
    that the user didn't dismiss."""
    return task_id is not None and task_status != "not_task"


# --- Thresholds -----------------------------------------------------------------


def thresholds_for(
    session: Session, source: str, version: str, settings: Settings | None = None
) -> Thresholds:
    settings = settings or get_settings()
    row = models_store.active(session, JEV_THRESHOLDS)
    params = (row.params or {}) if row is not None else {}
    if params.get("model") != settings.jev_model or params.get("questions") != version:
        return Thresholds()
    sources = params.get("sources") or {}
    return Thresholds().with_overrides({**sources.get("all", {}), **sources.get(source, {})})


def fit_threshold(rows: Iterable[tuple[float, bool]], precision: float) -> float | None:
    """None without enough rows to judge; `DISABLED` when no threshold reaches
    the target."""
    ordered = sorted(rows, key=lambda r: r[0], reverse=True)
    if len(ordered) < MIN_ROWS:
        return None
    best: float | None = None
    hits = 0
    for n, (p, correct) in enumerate(ordered, start=1):
        hits += correct
        # A threshold admits every row at or above it, so judge only after the
        # last row sharing this probability.
        if n < len(ordered) and ordered[n][0] == p:
            continue
        if n >= MIN_SUPPORT and hits / n >= precision:
            best = p
    return DISABLED if best is None else max(FLOOR, best)


def fit_thresholds(rows: Iterable[Row]) -> dict[str, dict[str, float]]:
    grouped: dict[tuple[str, str], list[tuple[float, bool]]] = defaultdict(list)
    for row in rows:
        grouped[(row.source, row.decision)].append((row.p, row.correct))
        grouped[("all", row.decision)].append((row.p, row.correct))
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (source, decision), values in grouped.items():
        threshold = fit_threshold(values, PRECISION[decision])
        if threshold is not None:
            out[source][decision] = round(threshold, 4)
    return dict(out)


def merge_thresholds(
    previous: Mapping[str, Mapping[str, float]], fitted: Mapping[str, Mapping[str, float]]
) -> dict[str, dict[str, float]]:
    """A fresh fit replaces a decision's threshold only where it had enough
    rows; elsewhere the previous value (e.g. bootstrapped from the replay)
    stays."""
    merged = {source: dict(values) for source, values in previous.items()}
    for source, values in fitted.items():
        merged.setdefault(source, {}).update(values)
    return merged


def _published_thresholds(
    session: Session, version: str, settings: Settings
) -> dict[str, dict[str, float]]:
    row = models_store.active(session, JEV_THRESHOLDS)
    params = (row.params or {}) if row is not None else {}
    if params.get("model") != settings.jev_model or params.get("questions") != version:
        return {}
    return dict(params.get("sources") or {})


def rows_from_trace(
    source: str,
    status: str,
    task_id,
    jev_trace: dict,
    task_status: str | None,
    weights: Mapping[str, float] | None = None,
) -> list[Row]:
    """The decisions one answered input can grade. `task_status` is the
    linked task's current status (a dismissal turns it to not_task). With
    `weights`, p(task) is recomputed from the stored features, so thresholds
    follow a freshly refit model."""
    answers = jev_trace.get("answers") or {}
    linked = linked_to_task(task_id, task_status)
    rows: list[Row] = []
    target = answers.get("target")
    matched = (jev_trace.get("candidates") or {}).get((target or {}).get("value"))
    if target and target.get("value") != "none":
        correct = status == "duplicate" and matched is not None and str(task_id) == matched
        rows.append(Row(source, "target", float(target["p"]), correct))
        action = answers.get("target_action") or {}
        if action.get("value") == "nothing_new":
            rows.append(Row(source, "no_change", float(action["p"]), correct))
    if weights and jev_trace.get("features"):
        p_task = predict(weights, jev_trace["features"])
    elif "p_task" in jev_trace:
        p_task = float(jev_trace["p_task"])
    else:
        return rows
    rows.append(Row(source, "drop", 1.0 - p_task, not linked))
    if not target or target.get("value") == "none":
        rows.append(Row(source, "create", p_task, linked and status != "duplicate"))
    return rows


# --- Nightly refit --------------------------------------------------------------


def _answered(
    session: Session, since: datetime, version: str
) -> list[tuple[Any, dict, str | None]]:
    """`(raw, jev trace, linked task status)` for inputs answered with `version`."""
    inputs = raw_inputs.jev_decided_since(session, since)
    statuses = tasks_store.latest_status_for(session, [r.task_id for r in inputs if r.task_id])
    out = []
    for raw in inputs:
        jev_trace = (raw.agent_trace or {}).get("jev") or {}
        if jev_trace.get("questions") == version and "answers" in jev_trace:
            out.append((raw, jev_trace, statuses.get(raw.task_id) if raw.task_id else None))
    return out


def refit(session: Session, now: datetime, settings: Settings | None = None) -> None:
    # Imported here: `app.agent` imports this module while it loads.
    from app.agent.triage.questions import QUESTIONS_VERSION

    settings = settings or get_settings()
    answered = _answered(session, now - WINDOW, QUESTIONS_VERSION)
    if not answered:
        return
    weights = refit_triage_model(session, answered, QUESTIONS_VERSION, settings)
    refit_thresholds(session, answered, QUESTIONS_VERSION, settings, weights)


def refit_triage_model(
    session: Session,
    answered: Sequence[tuple[Any, dict, str | None]],
    version: str,
    settings: Settings,
) -> dict[str, float] | None:
    rows = [
        (jev_trace["features"], int(linked_to_task(raw.task_id, task_status)))
        for raw, jev_trace, task_status in answered
        if jev_trace.get("features")
    ]
    positives = sum(label for _, label in rows)
    if len(rows) < MODEL_MIN_ROWS or positives < MODEL_MIN_POSITIVES:
        log.info("learn · jev triage model skipped rows=%d positives=%d", len(rows), positives)
        return triage_model_for(session, version, settings)
    weights = fit_logistic(rows)
    models_store.publish(
        session,
        kind=TRIAGE_MODEL,
        params={"model": settings.jev_model, "questions": version, "weights": weights},
        n_samples=len(rows),
        metrics={"positives": positives},
    )
    session.commit()
    log.info("learn · jev triage model refit rows=%d positives=%d", len(rows), positives)
    return weights


def refit_thresholds(
    session: Session,
    answered: Sequence[tuple[Any, dict, str | None]],
    version: str,
    settings: Settings,
    weights: Mapping[str, float] | None,
) -> dict:
    rows = [
        row
        for raw, jev_trace, task_status in answered
        for row in rows_from_trace(
            raw.source, raw.status, raw.task_id, jev_trace, task_status, weights
        )
    ]
    fitted = merge_thresholds(
        _published_thresholds(session, version, settings), fit_thresholds(rows)
    )
    models_store.publish(
        session,
        kind=JEV_THRESHOLDS,
        params={"model": settings.jev_model, "questions": version, "sources": fitted},
        n_samples=len(rows),
        metrics={"precision_targets": PRECISION},
    )
    session.commit()
    log.info("learn · jev thresholds refit rows=%d sources=%s", len(rows), sorted(fitted))
    return fitted
