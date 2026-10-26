"""Per-source Jev thresholds, refit nightly from how inputs actually ended.

Every input Jev answered (shadow or acting) is a labelled example once its
final status is known — user dismissals and promotions included, since they
rewrite the status. For each decision the threshold is the lowest probability
whose answers at or above it reach the decision's precision target. Without
enough rows the conservative defaults in `Thresholds` stay; a decision whose
answers never reach the target is switched off.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.clients import learned_models as models_store
from app.db.clients import raw_inputs, tasks as tasks_store

log = logging.getLogger(__name__)

JEV_THRESHOLDS = "jev_thresholds"
WINDOW = timedelta(days=180)
PRECISION = {"drop": 0.98, "create": 0.9, "target": 0.95, "no_change": 0.95}
MIN_ROWS = 50
MIN_SUPPORT = 10
FLOOR = 0.5
DISABLED = 1.01


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


def thresholds_for(session: Session, source: str, settings: Settings | None = None) -> Thresholds:
    settings = settings or get_settings()
    row = models_store.active(session, JEV_THRESHOLDS)
    if row is None or (row.params or {}).get("model") != settings.jev_model:
        return Thresholds()
    sources = (row.params or {}).get("sources") or {}
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


def rows_from_trace(
    source: str, status: str, task_id, jev_trace: dict, task_status: str | None
) -> list[Row]:
    """The decisions one answered input can grade. `task_status` is the
    linked task's current status (a dismissal turns it to not_task)."""
    answers = jev_trace.get("answers") or {}
    is_task_answer = answers.get("is_task")
    truth_task = task_id is not None and task_status != "not_task"
    rows: list[Row] = []
    target = answers.get("target")
    matched = (jev_trace.get("candidates") or {}).get((target or {}).get("value"))
    if target and target.get("value") != "none":
        correct = status == "duplicate" and matched is not None and str(task_id) == matched
        rows.append(Row(source, "target", float(target["p"]), correct))
        action = answers.get("target_action") or {}
        if action.get("value") == "nothing_new":
            rows.append(Row(source, "no_change", float(action["p"]), correct))
    if is_task_answer:
        p = float(is_task_answer["p"])
        p_not_task = p if is_task_answer.get("value") == "not_task" else 1.0 - p
        rows.append(Row(source, "drop", p_not_task, not truth_task))
        if not target or target.get("value") == "none":
            rows.append(
                Row(source, "create", 1.0 - p_not_task, truth_task and status != "duplicate")
            )
    return rows


def decision_rows(session: Session, since: datetime) -> list[Row]:
    inputs = raw_inputs.jev_decided_since(session, since)
    statuses = tasks_store.latest_status_for(session, [r.task_id for r in inputs if r.task_id])
    rows: list[Row] = []
    for raw in inputs:
        jev_trace = (raw.agent_trace or {}).get("jev") or {}
        if "answers" not in jev_trace:
            continue
        rows.extend(
            rows_from_trace(
                raw.source,
                raw.status,
                raw.task_id,
                jev_trace,
                statuses.get(raw.task_id) if raw.task_id else None,
            )
        )
    return rows


def refit_thresholds(session: Session, now: datetime, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    rows = decision_rows(session, now - WINDOW)
    fitted = fit_thresholds(rows)
    models_store.publish(
        session,
        kind=JEV_THRESHOLDS,
        params={"model": settings.jev_model, "sources": fitted},
        n_samples=len(rows),
        metrics={"precision_targets": PRECISION},
    )
    session.commit()
    log.info("learn · jev thresholds refit rows=%d sources=%s", len(rows), sorted(fitted))
    return fitted
