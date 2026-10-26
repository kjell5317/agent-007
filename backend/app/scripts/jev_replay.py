"""Replay processed inputs through Jev and compare with how they really ended.

Each input's triage state is rebuilt from what the agent saw at decision time
— the candidate refs stored in its trace, sender history before it arrived —
so later inputs can't leak in. Jev gets the live triage and field questions;
the report compares its answers and the original agent decision against the
final status (dismissals and promotions included) per source, then lists
precision and coverage per threshold for every decision triage acts on, and
label / estimate accuracy on inputs that became tasks. Writes nothing unless
`--csv` is given. Needs TYPESAFE_API_KEY; `jev_mode` doesn't matter.

    python -m app.scripts.jev_replay [--days 120] [--limit 500] [--concurrency 4] [--csv out.csv]
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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agent.helpers import jev
from app.agent.triage.questions import ESTIMATE_MINUTES, field_questions, triage_questions
from app.agent.triage.runner import sender_history_for
from app.agent.triage.state import new_input_state
from app.config import get_settings
from app.db import SessionLocal
from app.db.clients import labels as labels_store
from app.db.clients import tasks as tasks_store
from app.db.models.raw_input import RawInput
from app.services.learn.jev import Row, rows_from_trace
from app.timezones import user_tz

log = logging.getLogger(__name__)

THRESHOLDS = (0.7, 0.8, 0.9, 0.95, 0.97, 0.99)
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
    truth: str
    agent: str | None
    jev_trace: dict[str, Any]
    task: Any


def sample(session: Session, *, days: int, limit: int) -> list[RawInput]:
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
    return out


def truth_of(raw: RawInput, task_status: str | None) -> str:
    if raw.task_id is None or task_status == "not_task":
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


def prepare(session: Session, raw: RawInput, labels: dict[str, str]) -> tuple[dict, dict, dict]:
    """`(state, questions, candidate ids)` as triage would have built them."""
    trace = raw.agent_trace or {}
    now = raw.received_at.astimezone(user_tz())
    pairs, precedents = candidates_from_trace(session, trace)
    state, refs = new_input_state(
        raw,
        now=now,
        task_candidates=pairs,
        not_task_precedents=precedents,
        sender_history=sender_history_for(session, raw, now),
    )
    questions = {
        **(
            {}
            if raw.source == "manual"
            else triage_questions(
                {cid: task.title for cid, (_, task) in zip(refs.candidates, pairs)}
            )
        ),
        **field_questions(labels, year=now.year, urls=refs.urls),
    }
    return state, questions, {k: str(v) for k, v in refs.candidates.items()}


async def ask(state: dict, questions: dict, gate: asyncio.Semaphore) -> jev.JevResult | None:
    try:
        async with gate:
            return await jev.ask(state, questions, get_settings(), name="jev-replay")
    except jev.JevUnavailable as exc:
        log.warning("replay · jev failed %s", exc)
        return None


def jev_verdict(item: Replayed) -> str:
    answers = item.jev_trace["answers"]
    target = answers.get("target")
    if target and target["value"] != "none":
        return "duplicate"
    is_task = answers.get("is_task")
    return is_task["value"] if is_task else "task"


def report(items: Sequence[Replayed]) -> None:
    by_source: dict[str, list[Replayed]] = defaultdict(list)
    for item in items:
        by_source[item.raw.source].append(item)

    print("\n== Triage verdict vs final outcome (argmax, no thresholds) ==")
    for source, group in sorted(by_source.items()):
        if source == "manual":
            continue
        n = len(group)
        jev_ok = sum(jev_verdict(i) == i.truth for i in group)
        agent_known = [i for i in group if i.agent]
        agent_ok = sum(i.agent == i.truth for i in agent_known)
        dropped = sum(jev_verdict(i) == "not_task" and i.truth != "not_task" for i in group)
        print(
            f"{source:<7} n={n:<5} jev={jev_ok / n:.1%}  agent={agent_ok / max(1, len(agent_known)):.1%}"
            f" (n={len(agent_known)})  jev would drop {dropped} real task(s)"
        )

    rows: list[Row] = []
    for item in items:
        if item.raw.source == "manual":
            continue
        rows.extend(
            rows_from_trace(
                item.raw.source,
                item.raw.status,
                item.raw.task_id,
                item.jev_trace,
                "not_task" if item.truth == "not_task" and item.raw.task_id else None,
            )
        )
    print("\n== Precision / coverage per threshold (rows the decision applies to) ==")
    grouped: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        grouped[(row.decision, row.source)].append(row)
        grouped[(row.decision, "all")].append(row)
    for (decision, source), group in sorted(grouped.items()):
        cells = []
        for t in THRESHOLDS:
            above = [r for r in group if r.p >= t]
            precision = sum(r.correct for r in above) / len(above) if above else float("nan")
            cells.append(f"≥{t:.2f}: {precision:.0%}/{len(above) / len(group):.0%}")
        print(f"{decision:<10}{source:<7} n={len(group):<5} " + "  ".join(cells))

    tasks = [i for i in items if i.task is not None and i.truth != "not_task"]
    if tasks:
        print("\n== Fields on inputs that became tasks ==")
        labelled = [i for i in tasks if i.task.label and "label" in i.jev_trace["answers"]]
        label_ok = sum(i.jev_trace["answers"]["label"]["value"] == i.task.label for i in labelled)
        print(f"label     n={len(labelled)} jev={label_ok / max(1, len(labelled)):.1%}")
        jev_err, agent_err = [], []
        for i in tasks:
            final = i.task.estimation
            if not final:
                continue
            level = int(i.jev_trace["answers"]["estimate"]["value"])
            jev_err.append(abs(math.log2(ESTIMATE_MINUTES[level] / final)))
            if i.task.agent_estimation:
                agent_err.append(abs(math.log2(i.task.agent_estimation / final)))
        if jev_err:
            print(
                f"estimate  median |log2(est/final)| jev={statistics.median(jev_err):.2f} (n={len(jev_err)})"
                + (
                    f"  agent={statistics.median(agent_err):.2f} (n={len(agent_err)})"
                    if agent_err
                    else ""
                )
            )


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
        writer.writerow(
            ["raw_input_id", "source", "truth", "agent", "jev", "is_task_p", "label", "estimate"]
        )
        for i in items:
            answers = i.jev_trace["answers"]
            writer.writerow(
                [
                    i.raw.id,
                    i.raw.source,
                    i.truth,
                    i.agent or "",
                    jev_verdict(i),
                    (answers.get("is_task") or {}).get("p", ""),
                    answers["label"]["value"],
                    ESTIMATE_MINUTES[int(answers["estimate"]["value"])],
                ]
            )


async def _run(days: int, limit: int, concurrency: int, csv_path: str | None) -> None:
    if not get_settings().typesafe_api_key.strip():
        raise SystemExit("TYPESAFE_API_KEY is not set")
    with SessionLocal() as session:
        raws = sample(session, days=days, limit=limit)
        statuses = tasks_store.latest_status_for(session, [r.task_id for r in raws if r.task_id])
        labels = labels_store.agent_descriptions(session)
        print(f"replaying {len(raws)} inputs with {get_settings().jev_model}")
        # DB reads stay sequential on the one session; only the Jev calls fan out.
        prepared = [prepare(session, raw, labels) for raw in raws]
        gate = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(
            *(ask(state, questions, gate) for state, questions, _ in prepared)
        )
        replayed = [
            Replayed(
                raw=raw,
                truth=truth_of(raw, statuses.get(raw.task_id)),
                agent=AGENT_OUTCOMES.get(str((raw.agent_trace or {}).get("outcome"))),
                jev_trace={**result.trace(), "candidates": candidate_ids},
                task=tasks_store.get(session, raw.task_id) if raw.task_id else None,
            )
            for raw, (_, _, candidate_ids), result in zip(raws, prepared, results)
            if result is not None
        ]
        report(replayed)
        manual_latency(session, days)
        if csv_path:
            write_csv(csv_path, replayed)
            print(f"wrote {csv_path}")
        session.rollback()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--limit", type=int, default=500, help="most recent inputs to replay")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--csv", default=None, help="write per-input results here")
    args = parser.parse_args(argv)
    logging.basicConfig()
    asyncio.run(_run(args.days, args.limit, args.concurrency, args.csv))


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


if __name__ == "__main__":
    main()
