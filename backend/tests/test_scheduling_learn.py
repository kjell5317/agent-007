"""Preference posterior, duration correction, and log → training rows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from app.services.learn import data, duration, preference
from app.services.plan import features as F

NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def move(from_hour: int, to_hour: int) -> preference.Row:
    frm = {f"h_start:{from_hour}": 1.0, f"h_end:{from_hour}": 1.0}
    to = {f"h_start:{to_hour}": 1.0, f"h_end:{to_hour}": 1.0}
    x = {k: to.get(k, 0) - frm.get(k, 0) for k in set(frm) | set(to)}
    return preference.Row(x=x, y=1.0)


def test_no_data_means_the_prior():
    post = preference.fit([])
    prior = F.prior_mean()
    assert all(post.mean[name] == prior[name] for name in prior)


def test_planted_preference_is_recovered():
    # The user keeps dragging tasks from 10:00 to 15:00.
    post = preference.fit([move(10, 15)] * 40)
    gain = post.mean["h_start:15"] + post.mean["h_end:15"] - post.mean["h_start:10"] - post.mean["h_end:10"]
    assert gain > 1.5
    # Untouched features stay at the prior.
    assert post.mean["h_start:3"] == F.prior_mean()["h_start:3"]


def test_evening_can_win_without_a_cap():
    post = preference.fit([move(14, 21)] * 200)
    assert post.mean["h_start:21"] + post.mean["h_end:21"] > post.mean["h_start:14"] + post.mean["h_end:14"]


def test_posterior_narrows_as_data_grows():
    few = preference.fit([move(10, 15)] * 3)
    many = preference.fit([move(10, 15)] * 60)
    i_few = few.active.index("h_start:15")
    i_many = many.active.index("h_start:15")
    assert many.cov[i_many][i_many] < few.cov[i_few][i_few]


def test_draws_vary_unobserved_features_more():
    post = preference.fit([move(10, 15)] * 60)
    rng = np.random.default_rng(1)
    draws = [preference.draw(post, rng) for _ in range(300)]
    spread_observed = np.std([d["h_start:15"] for d in draws])
    spread_unobserved = np.std([d["h_start:2"] for d in draws])
    assert spread_observed < spread_unobserved
    assert "bias" not in draws[0]


def test_pointwise_rows_fit_with_a_bias():
    rows = [preference.Row(x={"h_start:9": 1.0, "bias": 1.0}, y=0.0)] * 10
    rows += [preference.Row(x={"h_start:14": 1.0, "bias": 1.0}, y=1.0)] * 10
    post = preference.fit(rows)
    assert post.mean["h_start:14"] > post.mean["h_start:9"]
    assert -10 < preference.log_likelihood(post, rows) < 0


def test_duration_identity_without_data():
    params = duration.fit([])
    assert duration.corrected_minutes(params, 60, "x") == 60


def test_duration_learns_an_underestimate():
    obs = [duration.Observation(60, 90, "Uni|cognitive") for _ in range(30)]
    params = duration.fit(obs)
    corrected = duration.corrected_minutes(params, 60, "Uni|cognitive")
    assert 80 < corrected < 92
    # Other groups only share the slope.
    assert abs(duration.corrected_minutes(params, 60, "Sport|physical") - 60) < 20


def test_a_new_task_gets_the_correction_once_and_keeps_the_raw_estimate(monkeypatch):
    from app.db.schemas.task import TaskCreate
    from app.services.learn import jobs

    params = duration.fit([duration.Observation(60, 90, "Uni|cognitive") for _ in range(30)])
    monkeypatch.setattr(jobs, "duration_params", lambda _s: params)
    task = jobs.with_learned_estimate(
        None, TaskCreate(title="Sheet", estimation=60, label="Uni", work_type="cognitive"),
    )
    assert 80 < task.estimation < 92 and task.agent_estimation == 60
    unset = TaskCreate(title="Sheet", label="Uni", work_type="cognitive")
    assert jobs.with_learned_estimate(None, unset) == unset


def test_upper_bounds_only_pull_down():
    # "Done" within a 60-minute slot: at most 60 — no evidence it was shorter.
    obs = [duration.Observation(60, 60, "g", upper_bound=True) for _ in range(30)]
    assert abs(duration.corrected_minutes(duration.fit(obs), 60, "g") - 60) < 1
    over = [duration.Observation(60, 40, "g", upper_bound=True) for _ in range(30)]
    assert duration.corrected_minutes(duration.fit(over), 60, "g") < 50


@dataclass
class LogRow:
    task_id: uuid.UUID
    kind: str
    slot_start: datetime | None
    slot_end: datetime | None
    features: dict | None
    created_at: datetime


def test_log_rows_to_training_rows():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    start = NOW - timedelta(days=1)
    end = start + timedelta(hours=1)
    snap = {"slot": {"h_start:13": 1.0}, "meta": {"estimate": 60, "label": "Uni", "work_type": None}}
    rows = [
        LogRow(a, "moved_by_user", start, end,
               {"slot": {"h_start:15": 1.0}, "prev": {"h_start:10": 1.0}}, NOW),
        LogRow(b, "checkin_sent", start, end, snap, end + timedelta(minutes=15)),
        LogRow(b, "done_confirmed", start, end, snap, end + timedelta(minutes=20)),
        LogRow(b, "closed", start, end, snap, end + timedelta(minutes=20)),
        LogRow(c, "checkin_sent", start, end, snap, end + timedelta(minutes=15)),
        LogRow(c, "still_working", start, end, snap, end + timedelta(minutes=16)),
        LogRow(c, "closed", start, end, snap, start + timedelta(minutes=95)),
        LogRow(d, "checkin_sent", start, end, snap, end + timedelta(minutes=15)),
        LogRow(d, "no_response", start, end, None, end + timedelta(hours=3)),
    ]
    pref = data.preference_rows(rows, NOW)
    pairwise = [r for r in pref if "bias" not in r.x]
    pointwise = [r for r in pref if "bias" in r.x]
    assert pairwise[0].x == {"h_start:15": 1.0, "h_start:10": -1.0}
    assert sorted(r.y for r in pointwise) == [1.0, 1.0]  # no_response is excluded

    obs = data.duration_observations(rows, NOW)
    exact = [o for o in obs if not o.upper_bound]
    upper = [o for o in obs if o.upper_bound]
    assert [round(o.actual) for o in exact] == [95]
    assert [round(o.actual) for o in upper] == [60]
    assert exact[0].group == "Uni|-"


def test_closed_outside_the_slot_is_not_an_observation():
    t = uuid.uuid4()
    start = NOW - timedelta(days=2)
    snap = {"slot": {"h_start:9": 1.0}, "meta": {"estimate": 30}}
    rows = [LogRow(t, "closed", start, start + timedelta(minutes=30), snap, NOW)]
    assert data.duration_observations(rows, NOW) == []
    assert data.preference_rows(rows, NOW) == []


def test_old_rows_weigh_less():
    assert data.decay(NOW - timedelta(days=90), NOW) == 0.5
