"""Fit, publish and serve the scheduler's learned parameters.

Slot preferences refit shortly after every labelled event and nightly; the
solver uses one posterior draw per local day (`weights_for`), so the schedule
doesn't flicker between triggers while exploration still happens day to day.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from app.db.clients import learned_models as models_store
from app.db.clients import schedule_log as log_store
from app.db.schemas.task import TaskCreate
from app.services.learn import data, duration, preference
from app.services.learn.jev import refit as refit_jev
from app.services.plan import features as F
from app.timezones import user_tz

log = logging.getLogger(__name__)

SLOT_PREFERENCE = "slot_preference"
SLOT_DRAW = "slot_draw"
DURATION = "duration"
CAPACITY = "deferral_capacity"

# Share of free good-hour time that goes to tasks, before any evidence; the
# pseudo-count is about one week of free good hours.
RHO_PRIOR = 0.5
RHO_PSEUDO_MINUTES = 5 * 9 * 60
RHO_WEEKS = 8
HELD_OUT_SHARE = 0.2
LOG_WINDOW = timedelta(days=730)
REFIT_DELAY_S = 30


def posterior(session: Session) -> preference.Posterior:
    row = models_store.active(session, SLOT_PREFERENCE)
    if row is None:
        return preference.prior()
    post = preference.Posterior.from_params(row.params, row.n_samples)
    # New features added after the fit start at their prior.
    for name, value in F.prior_mean().items():
        post.mean.setdefault(name, value)
    return post


def weights_for(session: Session, day: date) -> dict[str, float]:
    """Today's posterior draw, drawn on first use and kept for the day."""
    row = models_store.active(session, SLOT_DRAW)
    if row is not None and row.params.get("date") == day.isoformat():
        weights = dict(row.params["weights"])
        for name, value in F.prior_mean().items():
            weights.setdefault(name, value)
        return weights
    post = posterior(session)
    model = models_store.active(session, SLOT_PREFERENCE)
    version = model.version if model is not None else 0
    rng = np.random.default_rng(int(day.strftime("%Y%m%d")) * 1000 + version)
    weights = preference.draw(post, rng)
    models_store.publish(
        session,
        kind=SLOT_DRAW,
        params={"date": day.isoformat(), "weights": weights, "posterior_version": version},
        n_samples=post.n_samples,
    )
    session.commit()
    return weights


def refit_preferences(session: Session, now: datetime) -> preference.Posterior:
    from app.db.clients import commute_log as commute_log_store

    log_rows = log_store.since(session, now - LOG_WINDOW)
    rows = data.preference_rows(log_rows, now)
    rows += data.commute_rows(commute_log_store.since(session, now - LOG_WINDOW), now)
    # Oldest first (lowest decay weight), so the held-out split takes the newest.
    rows.sort(key=lambda row: row.weight)
    metrics: dict = {"rows": len(rows)}
    if len(rows) >= 5:
        # Chronological split: the newest 20 % measure how well older data
        # predicts new behaviour. A metric, not a gate.
        cut = int(len(rows) * (1 - HELD_OUT_SHARE))
        held_out = preference.fit(rows[:cut])
        metrics["held_out_log_likelihood"] = preference.log_likelihood(held_out, rows[cut:])
        metrics["held_out_prior_log_likelihood"] = preference.log_likelihood(
            preference.prior(), rows[cut:]
        )
    post = preference.fit(rows)
    models_store.publish(
        session, kind=SLOT_PREFERENCE, params=post.to_params(),
        n_samples=len(rows), metrics=metrics,
    )
    session.commit()
    log.info("learn · slot preferences refit rows=%d active=%d", len(rows), len(post.active))
    return post


def refit_duration(session: Session, now: datetime) -> dict:
    log_rows = log_store.since(session, now - LOG_WINDOW)
    observations = data.duration_observations(log_rows, now)
    params = duration.fit(observations)
    models_store.publish(session, kind=DURATION, params=params, n_samples=len(observations))
    session.commit()
    log.info("learn · duration refit observations=%d b=%.3f", len(observations), params["b"])
    return params


def duration_params(session: Session) -> dict | None:
    row = models_store.active(session, DURATION)
    return row.params if row is not None else None


def with_learned_estimate(session: Session, payload: TaskCreate) -> TaskCreate:
    """Correct an agent's duration estimate once, as the task is created; the
    planner then books it exactly as set. The raw estimate stays on the task
    as what the duration model trains on."""
    if not payload.estimation:
        return payload
    group = duration.group_key(payload.label, payload.work_type, payload.estimation_source)
    minutes = duration.corrected_minutes(duration_params(session), payload.estimation, group)
    return payload.model_copy(
        update={"estimation": round(minutes), "agent_estimation": payload.estimation},
    )


def rho(session: Session) -> float:
    row = models_store.active(session, CAPACITY)
    return float(row.params.get("rho", RHO_PRIOR)) if row is not None else RHO_PRIOR


async def refit_capacity(session: Session, now: datetime) -> float:
    """`ρ` = task minutes completed per week ÷ free good-hour minutes that
    week, over the last eight weeks, smoothed toward the prior."""
    from app.services.plan.snapshot import calendar_items
    from app.services.plan.timeline import free_good_minutes, good_hours

    tz = user_tz()
    today = now.astimezone(tz).date()
    first = today - timedelta(weeks=RHO_WEEKS)
    start = datetime.combine(first, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    items = await calendar_items(session, start, end)
    hours = good_hours(posterior(session).mean)
    free = sum(free_good_minutes(items, first, today - timedelta(days=1), hours, tz).values())
    done = completed_minutes(log_store.since(session, start), start, end)
    value = (done + RHO_PRIOR * RHO_PSEUDO_MINUTES) / (free + RHO_PSEUDO_MINUTES)
    models_store.publish(
        session, kind=CAPACITY,
        params={"rho": value, "completed_minutes": done, "free_minutes": free},
        n_samples=RHO_WEEKS,
    )
    session.commit()
    log.info("learn · capacity rho=%.3f done=%.0f free=%.0f", value, done, free)
    return value


def completed_minutes(log_rows, start: datetime, end: datetime) -> float:
    total = 0.0
    seen: set[tuple] = set()
    for row in log_rows:
        if row.kind not in {"done_confirmed", "closed"} or row.slot_start is None or row.slot_end is None:
            continue
        if not start <= row.created_at < end:
            continue
        key = (row.task_id, row.slot_start)
        if key in seen:
            continue
        seen.add(key)
        total += max(0.0, (row.slot_end - row.slot_start).total_seconds() / 60)
    return total


async def nightly(session: Session) -> None:
    now = datetime.now(user_tz())
    refit_preferences(session, now)
    refit_duration(session, now)
    try:
        await refit_capacity(session, now)
    except Exception as exc:  # noqa: BLE001 — keep the previous ρ
        log.warning("learn · capacity refit failed err=%s", exc)
        session.rollback()
    try:
        refit_jev(session, now)
    except Exception as exc:  # noqa: BLE001 — keep the previous thresholds
        log.warning("learn · jev refit failed err=%s", exc)
        session.rollback()
    stats = scheduling_stats(session, now=now)
    from app import observability

    with observability.root_span("scheduler.metrics", tags=["scheduler"]) as span:
        span.update(output=stats)


# --- Event-triggered refits --------------------------------------------------

_refit_task: asyncio.Task | None = None


def refit_soon() -> None:
    """Coalesce a burst of labelled events into one refit a little later."""
    global _refit_task
    if _refit_task is not None and not _refit_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _refit_task = loop.create_task(_refit_later())


async def _refit_later() -> None:
    from app.db import SessionLocal

    await asyncio.sleep(REFIT_DELAY_S)
    try:
        with SessionLocal() as session:
            now = datetime.now(user_tz())
            refit_preferences(session, now)
            refit_duration(session, now)
    except Exception:  # noqa: BLE001 — the nightly refit retries
        log.exception("learn · event refit failed")


# --- Metrics -------------------------------------------------------------------


def scheduling_stats(session: Session, *, now: datetime, weeks: int = 8) -> dict:
    tz = user_tz()
    today = now.astimezone(tz).date()
    week_start = today - timedelta(days=today.weekday())
    first = week_start - timedelta(weeks=weeks - 1)
    start = datetime.combine(first, datetime.min.time(), tzinfo=tz)
    rows = log_store.since(session, start)
    per_week: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    slots: dict[tuple, set[str]] = defaultdict(set)
    slot_week: dict[tuple, str] = {}
    for row in rows:
        week = row.created_at.astimezone(tz).date()
        week = (week - timedelta(days=week.weekday())).isoformat()
        if row.kind == "moved_by_user":
            per_week[week]["user_moves"] += 1
        if row.slot_start is not None:
            key = (row.task_id, row.slot_start)
            slots[key].add(row.kind)
            if row.kind in {"checkin_sent", "closed"}:
                slot_week.setdefault(key, week)
                if row.kind == "closed" and row.slot_end is not None and not (
                    row.slot_start <= row.created_at <= row.slot_end + data.CLOSE_GRACE
                ):
                    slots[key].add("closed_outside")
    for key, kinds in slots.items():
        week = slot_week.get(key)
        if week is None:
            continue
        counts = per_week[week]
        counts["slots"] += 1
        if "checkin_sent" in kinds:
            counts["checkins"] += 1
        if "not_done" in kinds:
            counts["not_done"] += 1
        if "no_response" in kinds:
            counts["no_response"] += 1
        if kinds & {"done_confirmed", "still_working"} or (
            "closed" in kinds and "closed_outside" not in kinds and "not_done" not in kinds
        ):
            counts["completed_in_slot"] += 1

    def rate(a: int, b: int) -> float | None:
        return round(a / b, 3) if b else None

    out = []
    for n in range(weeks):
        week = (first + timedelta(weeks=n)).isoformat()
        c = per_week.get(week, {})
        out.append({
            "week": week,
            "slots": c.get("slots", 0),
            "completed_in_slot_share": rate(c.get("completed_in_slot", 0), c.get("slots", 0)),
            "user_moves": c.get("user_moves", 0),
            "not_done_rate": rate(c.get("not_done", 0), c.get("checkins", 0)),
            "no_response_rate": rate(c.get("no_response", 0), c.get("checkins", 0)),
        })
    model = models_store.active(session, SLOT_PREFERENCE)
    return {
        "weeks": out,
        "model": {
            "version": model.version if model else None,
            "samples": model.n_samples if model else 0,
            "metrics": model.metrics if model else None,
        },
        "rho": rho(session),
    }
