"""Schedule log → training rows. Pure over log-row-like objects (anything
with `task_id`, `kind`, `slot_start`, `slot_end`, `features`, `created_at`)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from app.services.learn.duration import Observation, group_key
from app.services.learn.preference import BIAS, Row

HALF_LIFE = timedelta(days=90)
# A close this soon after the slot ended still counts as "inside the slot".
CLOSE_GRACE = timedelta(minutes=15)
# A cued slot with no answer this long after it ended was not started.
CUE_SETTLED = timedelta(minutes=15)
# Remembered times are less exact than a live session.
RETRO_WEIGHT = 0.5


def decay(created_at: datetime, now: datetime) -> float:
    age = max(timedelta(0), now - created_at)
    return 0.5 ** (age / HALF_LIFE)


def _by_slot(rows) -> dict[tuple, list]:
    out: dict[tuple, list] = defaultdict(list)
    for row in rows:
        if row.slot_start is not None:
            out[(row.task_id, row.slot_start)].append(row)
    return out


def _slot_features(rows) -> dict | None:
    for row in rows:
        feats = (row.features or {}).get("slot")
        if feats:
            return row.features
    return None


def _closed_in_slot(closed, start: datetime, end: datetime) -> bool:
    return start <= closed.created_at <= end + CLOSE_GRACE


def preference_rows(log_rows, now: datetime) -> list[Row]:
    rows: list[Row] = []
    for row in log_rows:
        if row.kind != "moved_by_user":
            continue
        feats = row.features or {}
        to, frm = feats.get("slot"), feats.get("prev")
        if not to or not frm:
            continue
        x = {name: to.get(name, 0.0) - frm.get(name, 0.0) for name in set(to) | set(frm)}
        x = {name: value for name, value in x.items() if value}
        if x:
            rows.append(Row(x=x, y=1.0, weight=decay(row.created_at, now)))

    for (_, _), slot_rows in _by_slot(log_rows).items():
        kinds = {r.kind for r in slot_rows}
        if "abandoned" in kinds:
            continue
        snapshot = _slot_features(slot_rows)
        if snapshot is None:
            continue
        closed = next((r for r in slot_rows if r.kind == "closed"), None)
        cue = next((r for r in slot_rows if r.kind == "cue_sent"), None)
        if kinds & {"cue_snoozed", "cue_declined"}:
            outcome = 0.0
        elif "started" in kinds:
            outcome = 1.0
        elif closed is not None and closed.slot_end is not None and _closed_in_slot(
            closed, closed.slot_start, closed.slot_end
        ):
            outcome = 1.0
        elif "rescheduled" in kinds:
            # Tapped away from: the task wasn't going to happen there.
            outcome = 0.0
        elif cue is not None and cue.slot_end is not None and cue.slot_end + CUE_SETTLED <= now:
            # Cued, and nothing came of it.
            outcome = 0.0
        else:
            continue
        stamp = max(r.created_at for r in slot_rows)
        rows.append(Row(
            x={**snapshot["slot"], BIAS: 1.0}, y=outcome, weight=decay(stamp, now),
        ))
    return rows


def duration_observations(log_rows, now: datetime) -> list[Observation]:
    out: list[Observation] = []
    for (task_id, slot_start), slot_rows in _by_slot(log_rows).items():
        kinds = {r.kind for r in slot_rows}
        if "abandoned" in kinds:
            continue
        snapshot = _slot_features(slot_rows) or {}
        meta = snapshot.get("meta") or next(
            ((r.features or {}).get("meta") for r in slot_rows if (r.features or {}).get("meta")), None
        )
        if not meta or not meta.get("estimate"):
            continue
        group = group_key(meta.get("label"), meta.get("work_type"), meta.get("estimator"))
        closed = next((r for r in slot_rows if r.kind == "closed"), None)
        stamp = max(r.created_at for r in slot_rows)
        weight = decay(stamp, now)
        if closed is not None and closed.slot_end is not None and _closed_in_slot(
            closed, slot_start, closed.slot_end
        ):
            bound = max(5.0, (closed.created_at - slot_start).total_seconds() / 60)
            out.append(Observation(meta["estimate"], bound, group, True, weight, task_id))
    return out


def session_observations(tasks, sessions_by_task, now: datetime) -> list[Observation]:
    """Closed tasks whose work was all recorded in sessions — the last one
    ended with Complete — so the worked minutes are the exact duration. A
    plain close after a paused session may hide unrecorded work, and a task
    created from logged work has no real estimate. `tasks` are `(task,
    attrs)` pairs with the label defaults applied, as in the feature
    snapshots."""
    out: list[Observation] = []
    for task, attrs in tasks:
        rows = sessions_by_task.get(task.id) or []
        if not rows or task.estimation_source == "logged":
            continue
        if any(r.ended_at is None or r.end_reason == "abandoned" for r in rows):
            continue
        if max(rows, key=lambda r: r.ended_at).end_reason != "completed":
            continue
        estimate = task.agent_estimation or task.estimation
        if not estimate:
            continue
        actual = sum(r.minutes for r in rows)
        if actual <= 0:
            continue
        weight = decay(max(r.ended_at for r in rows), now)
        if any(r.source == "retro" for r in rows):
            weight *= RETRO_WEIGHT
        estimator = task.estimation_source if task.agent_estimation else None
        out.append(Observation(
            estimate, actual, group_key(attrs.label, attrs.work_type, estimator), False, weight, task.id,
        ))
    return out


def commute_rows(flips, now: datetime) -> list[Row]:
    """A flip says the mode taken beat the mode planned: one pairwise row."""
    rows: list[Row] = []
    for flip in flips:
        feats = flip.features or {}
        taken, planned = feats.get("flipped"), feats.get("planned")
        if not taken or not planned:
            continue
        x = {name: taken.get(name, 0.0) - planned.get(name, 0.0) for name in set(taken) | set(planned)}
        x = {name: value for name, value in x.items() if value}
        if x:
            rows.append(Row(x=x, y=1.0, weight=decay(flip.created_at, now)))
    return rows
