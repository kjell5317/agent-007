"""Replay processed inputs through Jev, fit the triage model, and report how
it would have done against how inputs really ended.

Each input's state is rebuilt from what the agent saw at decision time — the
candidate refs stored in its trace, sender history before it arrived — so
later inputs can't leak in. Jev gets the live triage and field questions.

The report, per source:

- the outcome mix and the always-"not a task" baseline;
- Jev's both-orders choice, the triage model (out-of-fold: each input scored
  by a model fitted without its time block) and the original agent, against
  the final outcome. Misses are split into new tasks and follow-ups.
- precision / coverage per threshold for every decision triage acts on;
- labels, graded only where the final label is one Jev could pick;
- estimates against the user's own corrections and observed durations.

"Final outcome" is the agent's decision unless the user corrected it, so the
agent's own score is an upper bound; `--csv` dumps every input for reading
the disagreements. Writes nothing unless `--publish` (model + thresholds to
`learned_models`) or `--csv` is given. Needs TYPESAFE_API_KEY; `jev_mode`
doesn't matter.

    python -m app.scripts.jev_replay [--days 120] [--limit 500] [--concurrency 4]
        [--csv out.csv] [--publish]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import math
import statistics
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agent.helpers import jev
from app.agent.helpers.jev import Answer
from app.agent.triage.features import p_task_choice, triage_features
from app.agent.triage.questions import (
    ESTIMATE_MINUTES,
    QUESTIONS_VERSION,
    field_questions,
    triage_questions,
)
from app.agent.triage.runner import sender_history_for
from app.agent.triage.state import new_input_state
from app.config import get_settings
from app.db import SessionLocal
from app.db.clients import labels as labels_store
from app.db.clients import learned_models as models_store
from app.db.clients import schedule_log as log_store
from app.db.clients import tasks as tasks_store
from app.db.models.raw_input import RawInput
from app.db.models.scheduling import TaskOverride
from app.services.learn import data as learn_data
from app.services.learn.jev import (
    JEV_THRESHOLDS,
    PRECISION,
    TRIAGE_MODEL,
    Row,
    fit_logistic,
    fit_thresholds,
    linked_to_task,
    predict,
    rows_from_trace,
)
from app.timezones import user_tz

log = logging.getLogger(__name__)

THRESHOLDS = (0.7, 0.8, 0.9, 0.95, 0.97, 0.99)
FOLDS = 5
AGENT_OUTCOMES = {
    "task_created": "task",
    "not_task": "not_task",
    "no_change": "duplicate",
    "updated": "duplicate",
    "closed": "duplicate",
    "reopened": "duplicate",
}


@dataclass
class Replayed:
    raw: RawInput
    truth: str  # task | duplicate | not_task
    agent: str | None
    answers: dict[str, Answer]
    jev_trace: dict[str, Any]
    features: dict[str, float]
    task: Any
    p_model: float | None = None

    @property
    def linked(self) -> bool:
        return self.truth != "not_task"

    @property
    def p_choice(self) -> float:
        return p_task_choice(self.answers)


@dataclass
class Prepared:
    state: dict
    questions: dict
    candidate_ids: dict[str, str]
    history: dict[str, int]
    hits: list


def sample(session: Session, *, days: int, limit: int) -> list[RawInput]:
    """The most recent inputs plus every user-corrected one, oldest first."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = select(RawInput).where(
        RawInput.processed_at.is_not(None),
        RawInput.received_at >= since,
        RawInput.source.in_(("gmail", "slack", "manual")),
    )
    corrected = list(
        session.scalars(base.where(text("(raw_inputs.agent_trace::jsonb) ? 'manual_override'")))
    )
    recent = list(session.scalars(base.order_by(RawInput.received_at.desc()).limit(limit)))
    seen: set[uuid.UUID] = set()
    out = []
    for raw in [*corrected, *recent]:
        if raw.id not in seen:
            seen.add(raw.id)
            out.append(raw)
    return sorted(out, key=lambda r: r.received_at)


def truth_of(raw: RawInput, task_status: str | None) -> str:
    if not linked_to_task(raw.task_id, task_status):
        return "not_task"
    return "duplicate" if raw.status == "duplicate" else "task"


def candidates_from_trace(session: Session, trace: dict) -> tuple[list, list]:
    """The (hit, task) pairs and NOT_TASK precedents the agent saw."""
    refs = trace.get("candidates") or trace.get("evidence_refs") or []
    pairs, precedents = [], []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        hit = SimpleNamespace(
            status=ref.get("status"),
            similarity=float(ref.get("similarity") or 0.0),
            sender=ref.get("sender"),
            subject=ref.get("title"),
            content_snippet=ref.get("snippet"),
            received_at=_parse(ref.get("received_at")),
            agent_trace={},
        )
        if ref.get("status") in ("open", "closed") and ref.get("task_id"):
            task = tasks_store.get(session, uuid.UUID(str(ref["task_id"])))
            if task is not None:
                pairs.append((hit, task))
        elif ref.get("status") == "not_task":
            precedents.append(hit)
    return pairs[:5], precedents


def prepare(session: Session, raw: RawInput, labels: dict[str, str]) -> Prepared:
    trace = raw.agent_trace or {}
    now = raw.received_at.astimezone(user_tz())
    pairs, precedents = candidates_from_trace(session, trace)
    history = sender_history_for(session, raw, now)
    state, refs = new_input_state(
        raw,
        now=now,
        task_candidates=pairs,
        not_task_precedents=precedents,
        sender_history=history,
    )
    triage = (
        {}
        if raw.source == "manual"
        else triage_questions({cid: task.title for cid, (_, task) in zip(refs.candidates, pairs)})
    )
    return Prepared(
        state=state,
        questions={**triage, **field_questions(labels, year=now.year, urls=refs.urls)},
        candidate_ids={k: str(v) for k, v in refs.candidates.items()},
        history=history,
        hits=[hit for hit, _ in pairs] + precedents,
    )


async def ask(state: dict, questions: dict, gate: asyncio.Semaphore) -> jev.JevResult | None:
    try:
        async with gate:
            return await jev.ask(state, questions, get_settings(), name="jev-replay")
    except jev.JevUnavailable as exc:
        log.warning("replay · jev failed %s", exc)
        return None


# --- Triage model ---------------------------------------------------------------


def out_of_fold(items: Sequence[Replayed]) -> None:
    """Score every Gmail/Slack input with a model fitted on the other time blocks."""
    triage = [i for i in items if i.raw.source != "manual"]
    if not triage:
        return
    size = math.ceil(len(triage) / FOLDS)
    for start in range(0, len(triage), size):
        train = triage[:start] + triage[start + size :]
        rows = [(i.features, int(i.linked)) for i in train]
        if sum(label for _, label in rows) < 5:
            continue
        weights = fit_logistic(rows)
        for item in triage[start : start + size]:
            item.p_model = predict(weights, item.features)


def threshold_rows(
    items: Sequence[Replayed], p_of: Callable[[Replayed], float | None]
) -> list[Row]:
    rows: list[Row] = []
    for item in items:
        p = p_of(item) if item.raw.source != "manual" else None
        if p is None:
            continue
        rows.extend(
            rows_from_trace(
                item.raw.source,
                item.raw.status,
                item.raw.task_id,
                {**item.jev_trace, "p_task": p},
                "not_task" if item.truth == "not_task" and item.raw.task_id else None,
            )
        )
    return rows


# --- Report ---------------------------------------------------------------------


def _pct(n: int, d: int) -> str:
    return f"{n / d:.1%}" if d else "n/a"


def _median(values: list[float]) -> str:
    return f"{statistics.median(values):.2f}" if values else "n/a"


def triage_report(items: Sequence[Replayed]) -> None:
    by_source: dict[str, list[Replayed]] = defaultdict(list)
    for item in items:
        if item.raw.source != "manual":
            by_source[item.raw.source].append(item)

    print("\n== Triage: ends up linked to a task, or not (at p = 0.5) ==")
    for source, group in sorted(by_source.items()):
        n = len(group)
        tasks = sum(i.truth == "task" for i in group)
        dups = sum(i.truth == "duplicate" for i in group)
        print(
            f"{source}: n={n}  new tasks={tasks}  follow-ups={dups}  not a task={n - tasks - dups}"
        )
        print(f"  {'always not a task':<22}{_pct(n - tasks - dups, n)}")
        scorers: list[tuple[str, Callable[[Replayed], float | None]]] = [
            ("jev choice", lambda i: i.p_choice),
            ("triage model (oof)", lambda i: i.p_model),
        ]
        for name, p_of in scorers:
            scored = [i for i in group if p_of(i) is not None]
            correct = sum((p_of(i) >= 0.5) == i.linked for i in scored)
            missed_tasks = sum(p_of(i) < 0.5 and i.truth == "task" for i in scored)
            missed_dups = sum(p_of(i) < 0.5 and i.truth == "duplicate" for i in scored)
            print(
                f"  {name:<22}{_pct(correct, len(scored))} (n={len(scored)})"
                f"  misses {missed_tasks} new task(s), {missed_dups} follow-up(s)"
            )
        known = [i for i in group if i.agent]
        agent_ok = sum((i.agent != "not_task") == i.linked for i in known)
        print(f"  {'agent (upper bound)':<22}{_pct(agent_ok, len(known))} (n={len(known)})")


def threshold_report(items: Sequence[Replayed]) -> None:
    scorers: list[tuple[str, Callable[[Replayed], float | None]]] = [
        ("triage model (out-of-fold)", lambda i: i.p_model),
        ("jev choice", lambda i: i.p_choice),
    ]
    for title, p_of in scorers:
        grouped: dict[tuple[str, str], list[Row]] = defaultdict(list)
        for row in threshold_rows(items, p_of):
            grouped[(row.decision, row.source)].append(row)
            grouped[(row.decision, "all")].append(row)
        print(f"\n== Precision / coverage per threshold — {title} ==")
        for (decision, source), group in sorted(grouped.items()):
            cells = []
            for t in THRESHOLDS:
                above = [r for r in group if r.p >= t]
                precision = sum(r.correct for r in above) / len(above) if above else float("nan")
                cells.append(f"≥{t:.2f}: {precision:.0%}/{len(above) / len(group):.0%}")
            print(
                f"{decision:<10}{source:<7} n={len(group):<5} target {PRECISION[decision]:.0%}  "
                + "  ".join(cells)
            )


def model_report(items: Sequence[Replayed]) -> dict[str, float] | None:
    rows = [(i.features, int(i.linked)) for i in items if i.raw.source != "manual"]
    if sum(label for _, label in rows) < 5:
        print("\n== Triage model: too few tasks to fit ==")
        return None
    weights = fit_logistic(rows)
    print(f"\n== Triage model fitted on all {len(rows)} inputs (largest weights first) ==")
    for name, weight in sorted(weights.items(), key=lambda kv: -abs(kv[1])):
        print(f"  {name:<22}{weight:+.2f}")
    return weights


def label_report(items: Sequence[Replayed], catalog: set[str]) -> None:
    tasks = [i for i in items if i.task is not None and i.task.label and i.linked]
    gradable = [i for i in tasks if i.task.label in catalog]
    print("\n== Labels (graded only where the final label is one Jev could pick) ==")
    print(
        f"tasks with a label: {len(tasks)}; label not in today's catalog or without "
        f"description: {len(tasks) - len(gradable)}"
    )
    for floor in (0.0, 0.6, 0.8):
        above = [i for i in gradable if i.answers["label"].p >= floor]
        ok = sum(i.answers["label"].value == i.task.label for i in above)
        print(
            f"  p ≥ {floor:.1f}: {_pct(ok, len(above))} correct, "
            f"covers {_pct(len(above), len(gradable))}"
        )


def estimate_report(items: Sequence[Replayed], corrected: dict, observed: dict) -> None:
    """`corrected` / `observed` map task id → (truth minutes, the agent's
    estimate or None); the third line only shows agreement with the final value."""
    print("\n== Estimates: median |log2(estimate / truth)| — 1.0 means off by 2× ==")
    truths: list[tuple[str, dict | None]] = [
        ("your corrections", corrected),
        ("observed durations", observed),
        ("final value (≈ agreement with the agent)", None),
    ]
    for title, truth in truths:
        jev_err, agent_err = [], []
        for item in items:
            task = item.task
            if task is None or not item.linked:
                continue
            actual, agent = (
                (task.estimation, None) if truth is None else truth.get(task.id, (None, None))
            )
            if not actual:
                continue
            minutes = ESTIMATE_MINUTES[int(item.answers["estimate"].value)]
            jev_err.append(abs(math.log2(minutes / actual)))
            agent = agent or (task.agent_estimation if truth is observed else None)
            if agent:
                agent_err.append(abs(math.log2(agent / actual)))
        line = f"  vs {title:<42} jev={_median(jev_err)} (n={len(jev_err)})"
        if truth is not None:
            line += f"  agent={_median(agent_err)} (n={len(agent_err)})"
        print(line)


def corrected_estimates(session: Session) -> dict:
    """Per task: the last estimate the user set by hand (UI edit or calendar
    resize) and the agent's value it replaced."""
    rows = session.scalars(
        select(TaskOverride)
        .where(TaskOverride.field == "estimation", TaskOverride.source.in_(("ui", "calendar")))
        .order_by(TaskOverride.created_at)
    )
    out: dict = {}
    for row in rows:
        if not isinstance(row.user_value, (int, float)):
            continue
        first_agent = out.get(row.task_id, (None, row.agent_value))[1]
        agent = first_agent if isinstance(first_agent, (int, float)) else None
        out[row.task_id] = (row.user_value, agent)
    return out


def observed_durations(session: Session) -> dict:
    """Per task: the exact duration from a "still working → done" check-in."""
    now = datetime.now(user_tz())
    observations = learn_data.duration_observations(
        log_store.since(session, now - timedelta(days=730)), now
    )
    return {o.task_id: (o.actual, None) for o in observations if not o.upper_bound and o.task_id}


def manual_latency(session: Session, days: int) -> None:
    row = session.execute(
        text(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY extract(epoch FROM processed_at - received_at)) AS p50,
                   count(*) AS n
            FROM raw_inputs
            WHERE source = 'manual' AND processed_at IS NOT NULL
              AND received_at >= now() - make_interval(days => :days)
              AND processed_at - received_at < interval '10 minutes'
            """
        ),
        {"days": days},
    ).one()
    if row.n:
        print(f"\nmanual creation P50 latency: {row.p50:.1f}s over {row.n} inputs")


def write_csv(path: str, items: Sequence[Replayed]) -> None:
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "raw_input_id", "source", "received_at", "truth", "agent", "p_choice", "p_model",
            "subject", "label_jev", "label_final", "estimate_jev",
        ])  # fmt: skip
        for i in items:
            meta = i.raw.source_metadata or {}
            subject = meta.get("subject") or next(
                (line for line in (i.raw.content or "").splitlines() if line.strip()), ""
            )
            writer.writerow([
                i.raw.id, i.raw.source, i.raw.received_at.isoformat(), i.truth, i.agent or "",
                round(i.p_choice, 3) if i.raw.source != "manual" else "",
                round(i.p_model, 3) if i.p_model is not None else "",
                " ".join(str(subject).split())[:120],
                i.answers["label"].value, i.task.label if i.task is not None else "",
                ESTIMATE_MINUTES[int(i.answers["estimate"].value)],
            ])  # fmt: skip


def publish(session: Session, items: Sequence[Replayed], weights: dict[str, float]) -> None:
    """Bootstrap live triage: the model fitted on all inputs, and thresholds
    fitted on its out-of-fold scores. The nightly refit replaces each only
    once live traces carry enough rows."""
    settings = get_settings()
    rows = threshold_rows(items, lambda i: i.p_model)
    sources = fit_thresholds(rows)
    common = {"model": settings.jev_model, "questions": QUESTIONS_VERSION}
    fitted_on = sum(1 for i in items if i.raw.source != "manual")
    models_store.publish(
        session,
        kind=TRIAGE_MODEL,
        params={**common, "weights": weights},
        n_samples=fitted_on,
        metrics={"source": "replay"},
    )
    models_store.publish(
        session,
        kind=JEV_THRESHOLDS,
        params={**common, "sources": sources},
        n_samples=len(rows),
        metrics={"source": "replay"},
    )
    session.commit()
    print(f"\npublished {TRIAGE_MODEL} (n={fitted_on}) and {JEV_THRESHOLDS}: {sources}")


async def _run(
    days: int, limit: int, concurrency: int, csv_path: str | None, do_publish: bool
) -> None:
    settings = get_settings()
    if not settings.typesafe_api_key.strip():
        raise SystemExit("TYPESAFE_API_KEY is not set")
    with SessionLocal() as session:
        raws = sample(session, days=days, limit=limit)
        statuses = tasks_store.latest_status_for(session, [r.task_id for r in raws if r.task_id])
        labels = labels_store.agent_descriptions(session)
        print(
            f"replaying {len(raws)} inputs with {settings.jev_model}, questions v{QUESTIONS_VERSION}"
        )
        # DB reads stay sequential on the one session; only the Jev calls fan out.
        prepared = [prepare(session, raw, labels) for raw in raws]
        gate = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(*(ask(p.state, p.questions, gate) for p in prepared))
        items = [
            Replayed(
                raw=raw,
                truth=truth_of(raw, statuses.get(raw.task_id)),
                agent=AGENT_OUTCOMES.get(str((raw.agent_trace or {}).get("outcome"))),
                answers=result.answers,
                jev_trace={**result.trace(), "candidates": prep.candidate_ids},
                features=triage_features(
                    result.answers,
                    source=raw.source,
                    meta=raw.source_metadata or {},
                    sender_history=prep.history,
                    candidates=prep.hits,
                ),
                task=tasks_store.get(session, raw.task_id) if raw.task_id else None,
            )
            for raw, prep, result in zip(raws, prepared, results)
            if result is not None
        ]
        out_of_fold(items)
        triage_report(items)
        threshold_report(items)
        weights = model_report(items)
        label_report(items, set(labels))
        estimate_report(items, corrected_estimates(session), observed_durations(session))
        manual_latency(session, days)
        if csv_path:
            write_csv(csv_path, items)
            print(f"wrote {csv_path}")
        if do_publish and weights:
            publish(session, items, weights)
        else:
            session.rollback()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--limit", type=int, default=500, help="most recent inputs to replay")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--csv", default=None, help="write per-input results here")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="store the fitted triage model and thresholds in learned_models (writes to the DB)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig()
    asyncio.run(_run(args.days, args.limit, args.concurrency, args.csv, args.publish))


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


if __name__ == "__main__":
    main()
