"""Slot features for the preference model — pure.

One sparse `{name: value}` vector per (task, slot, surroundings). The same
helpers produce the log snapshots the learner trains on and the cost tables
the solver optimises, so training and serving can't drift apart.

Utilities are log-odds. `PRIOR_MEAN` approximates the rules of the legacy
planner (day windows, the 15-minute event buffer, earlier days first); the
learner pulls the weights away from it as evidence accrues, at a speed set
per feature group by `PRIOR_SIGMA`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

WORK_TYPES = ("physical", "cognitive", "social")
EFFORTS = ("low", "medium", "high")
DEADLINE_KINDS = ("hard", "firm", "soft")
SIDES = ("prev", "next")

# A neighbour further away than this counts as "none".
NEIGHBOUR_MINUTES = 60
GAP_BUCKETS = ("0", "5", "10", "15+")
HTD_BUCKETS = ("0-6", "6-24", "24-72", "72+")
HTD_HOURS = (6, 24, 72)
LOAD_BUCKETS = ("<240", "240-360", "360-480", ">480")
LOAD_MINUTES = (240, 360, 480)
MEETING_BUCKETS = ("<60", "60-180", "180-300", ">300")
MEETING_MINUTES = (60, 180, 300)

COMMUTE_MODES = ("bicycling", "transit")
# Ride-length bucket bounds as multiples of `commute_bike_max_minutes`
# (15 / 25 / 35 / 45 min at the default 25).
RIDE_FACTORS = (0.6, 1.0, 1.4, 1.8)
RIDE_BUCKETS = 5
RAIN_BUCKETS = 5


@dataclass(frozen=True)
class Neighbour:
    kind: str  # "event" | "task"
    gap_min: float
    label: str | None = None
    work_type: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class SlotContext:
    start: datetime  # user-local, aware
    end: datetime
    now: datetime
    due: datetime
    deadline_kind: str = "firm"
    label: str | None = None
    work_type: str | None = None
    effort: str | None = None
    prev: Neighbour | None = None
    next: Neighbour | None = None
    day_load_min: float = 0.0
    meeting_min: float = 0.0


def hour_prior(hour: int) -> float:
    if 10 <= hour <= 18:
        return 0.0
    if 8 <= hour <= 9 or 19 <= hour <= 23:
        return -2.0
    return -8.0


def gap_bucket(gap_min: float | None) -> str:
    if gap_min is None or gap_min > 10:
        return "15+"
    if gap_min <= 0:
        return "0"
    return "5" if gap_min <= 5 else "10"


def htd_bucket(hours: float) -> str:
    for bucket, limit in zip(HTD_BUCKETS, HTD_HOURS):
        if hours < limit:
            return bucket
    return HTD_BUCKETS[-1]


def load_bucket(minutes: float) -> str:
    for bucket, limit in zip(LOAD_BUCKETS, LOAD_MINUTES):
        if minutes < limit:
            return bucket
    return LOAD_BUCKETS[-1]


def end_hour(end: datetime) -> int:
    return (end - timedelta(minutes=1)).hour


# --- Component features (shared with the solver's cost tables) ---------------


def start_hour_features(hour: int, work_type: str | None, effort: str | None) -> dict[str, float]:
    out = {f"h_start:{hour}": 1.0}
    if work_type:
        out[f"h_wt:{hour}:{work_type}"] = 1.0
    if effort:
        out[f"h_eff:{hour}:{effort}"] = 1.0
    return out


def end_hour_features(hour: int) -> dict[str, float]:
    return {f"h_end:{hour}": 1.0}


def meeting_bucket(minutes: float) -> str:
    for bucket, limit in zip(MEETING_BUCKETS, MEETING_MINUTES):
        if minutes < limit:
            return bucket
    return MEETING_BUCKETS[-1]


def day_features(
    day: date, today: date, work_type: str | None, meeting_min: float = 0.0
) -> dict[str, float]:
    weekday = day.weekday()
    angle = 2 * math.pi * (day.timetuple().tm_yday - 1) / 365.25
    out = {
        f"wd:{weekday}": 1.0,
        "season_sin": math.sin(angle),
        "season_cos": math.cos(angle),
        f"meet:{meeting_bucket(meeting_min)}": 1.0,
    }
    days = (day - today).days
    if days:
        out["days_from_now"] = float(days)
    if work_type:
        out[f"wd_wt:{weekday}:{work_type}"] = 1.0
    return out


def gap_features(side: str, gap_min: float | None) -> dict[str, float]:
    return {f"gap_{side}:{gap_bucket(gap_min)}": 1.0}


def neighbour_features(
    side: str,
    neighbour: Neighbour | None,
    *,
    label: str | None,
    work_type: str | None,
    effort: str | None,
) -> dict[str, float]:
    if neighbour is None or neighbour.gap_min > NEIGHBOUR_MINUTES:
        return {f"nb_{side}:none": 1.0}
    out = {f"nb_{side}:{neighbour.kind}": 1.0}
    if label and neighbour.label == label:
        out[f"nb_{side}:same_label"] = 1.0
    if work_type and neighbour.work_type:
        out[f"nb_{side}:wt:{work_type}:{neighbour.work_type}"] = 1.0
    if effort and neighbour.effort:
        out[f"nb_{side}:eff:{effort}:{neighbour.effort}"] = 1.0
    return out


def late_flag_key(kind: str) -> str:
    return f"late:{kind}"


def late_hours_key(kind: str) -> str:
    return f"late_h:{kind}"


def htd_key(bucket: str) -> str:
    return f"htd:{bucket}"


def load_key(bucket: str) -> str:
    return f"load:{bucket}"


def deadline_features(end: datetime, due: datetime, kind: str) -> dict[str, float]:
    """Lateness per deadline kind plus hours-to-deadline buckets. A late slot
    also falls in the closest bucket — the solver encodes the buckets as
    "ends later than due − 6 h" thresholds, which a late slot satisfies."""
    hours = (due - end).total_seconds() / 3600
    out = {htd_key(htd_bucket(hours)): 1.0}
    if hours < 0:
        out[late_flag_key(kind)] = 1.0
        out[late_hours_key(kind)] = -hours
    return out


def load_features(minutes: float) -> dict[str, float]:
    return {load_key(load_bucket(minutes)): 1.0}


def features(ctx: SlotContext) -> dict[str, float]:
    out: dict[str, float] = {}
    out.update(start_hour_features(ctx.start.hour, ctx.work_type, ctx.effort))
    out.update(end_hour_features(end_hour(ctx.end)))
    out.update(day_features(ctx.start.date(), ctx.now.date(), ctx.work_type, ctx.meeting_min))
    for side, neighbour in (("prev", ctx.prev), ("next", ctx.next)):
        out.update(gap_features(side, neighbour.gap_min if neighbour else None))
        out.update(neighbour_features(
            side, neighbour, label=ctx.label, work_type=ctx.work_type, effort=ctx.effort,
        ))
    out.update(deadline_features(ctx.end, ctx.due, ctx.deadline_kind))
    out.update(load_features(ctx.day_load_min))
    return out


# --- Commute legs -------------------------------------------------------------


@dataclass(frozen=True)
class LegWeather:
    rain_pct: float = 0.0
    rain_mm: float = 0.0
    temp_c: float | None = None
    wind_kmh: float | None = None
    dark: bool = False


def ride_bucket(minutes: float, bike_max_minutes: float) -> int:
    for n, factor in enumerate(RIDE_FACTORS):
        if minutes <= factor * bike_max_minutes:
            return n
    return len(RIDE_FACTORS)


def rain_bucket(pct: float, threshold_pct: float) -> int:
    """Bounds at a third, two thirds, the threshold itself and 20 points
    above it (10 / 20 / 30 / 50 % at the default 30)."""
    bounds = (threshold_pct / 3, 2 * threshold_pct / 3, threshold_pct, threshold_pct + 20)
    for n, bound in enumerate(bounds):
        if pct < bound:
            return n
    return len(bounds)


def ride_weather_features(weather: LegWeather | None, rain_threshold_pct: float) -> dict[str, float]:
    """The weather part of riding at a given hour — the same for every leg,
    so the solver keeps it in one table per hour."""
    if weather is None:
        return {}
    out = {f"cm_rain:{rain_bucket(weather.rain_pct, rain_threshold_pct)}": 1.0}
    if weather.rain_mm > 0:
        out["cm_rain_mm"] = weather.rain_mm
    if weather.temp_c is not None and weather.temp_c < 5:
        out["cm_cold"] = 5 - weather.temp_c
    if weather.wind_kmh is not None and weather.wind_kmh > 25:
        out["cm_wind"] = weather.wind_kmh - 25
    if weather.dark:
        out["cm_dark"] = 1.0
    return out


def leg_features(
    mode: str,
    minutes: float,
    *,
    weather: LegWeather | None = None,
    after_physical: bool = False,
    bike_max_minutes: float = 25,
    rain_threshold_pct: float = 30,
) -> dict[str, float]:
    if mode != "bicycling":
        return {"cm_min:transit": minutes, "cm_transit": 1.0}
    out = {
        "cm_min:bicycling": minutes,
        f"cm_ride:{ride_bucket(minutes, bike_max_minutes)}": 1.0,
    }
    out.update(ride_weather_features(weather, rain_threshold_pct))
    if after_physical:
        out["cm_after_physical"] = 1.0
    return out


def utility(feats: dict[str, float], weights: dict[str, float]) -> float:
    return sum(value * weights.get(name, 0.0) for name, value in feats.items())


# --- Prior -------------------------------------------------------------------


def feature_names() -> list[str]:
    """Every feature the model knows, in a stable order."""
    names: list[str] = []
    for hour in range(24):
        names.append(f"h_start:{hour}")
        names.append(f"h_end:{hour}")
        names.extend(f"h_wt:{hour}:{wt}" for wt in WORK_TYPES)
        names.extend(f"h_eff:{hour}:{eff}" for eff in EFFORTS)
    for weekday in range(7):
        names.append(f"wd:{weekday}")
        names.extend(f"wd_wt:{weekday}:{wt}" for wt in WORK_TYPES)
    names.extend(["season_sin", "season_cos", "days_from_now"])
    for side in SIDES:
        names.extend(f"gap_{side}:{bucket}" for bucket in GAP_BUCKETS)
        names.extend(f"nb_{side}:{kind}" for kind in ("none", "event", "task", "same_label"))
        names.extend(f"nb_{side}:wt:{a}:{b}" for a in WORK_TYPES for b in WORK_TYPES)
        names.extend(f"nb_{side}:eff:{a}:{b}" for a in EFFORTS for b in EFFORTS)
    for kind in DEADLINE_KINDS:
        names.extend([late_flag_key(kind), late_hours_key(kind)])
    names.extend(htd_key(bucket) for bucket in HTD_BUCKETS)
    names.extend(load_key(bucket) for bucket in LOAD_BUCKETS)
    names.extend(f"meet:{bucket}" for bucket in MEETING_BUCKETS)
    names.extend(f"cm_min:{mode}" for mode in COMMUTE_MODES)
    names.append("cm_transit")
    names.extend(f"cm_ride:{n}" for n in range(RIDE_BUCKETS))
    names.extend(f"cm_rain:{n}" for n in range(RAIN_BUCKETS))
    names.extend(["cm_rain_mm", "cm_cold", "cm_wind", "cm_dark", "cm_after_physical"])
    return names


_GAP_PRIOR = {"0": -3.0, "5": -2.0, "10": -1.0, "15+": 0.0}
_LATE_PRIOR = {
    late_flag_key("hard"): -1000.0,
    late_hours_key("hard"): -0.5,
    late_flag_key("firm"): -10.0,
    late_hours_key("firm"): -0.5,
    late_flag_key("soft"): 0.0,
    late_hours_key("soft"): -0.05,
}
_LOAD_PRIOR = {"<240": 0.0, "240-360": 0.0, "360-480": -1.0, ">480": -3.0}
# Today's hard rules as a prior: a step at the bike threshold and at the rain
# threshold, travel time and a fixed transit penalty around them.
_COMMUTE_PRIOR = {
    "cm_min:bicycling": -0.02,
    "cm_min:transit": -0.02,
    "cm_transit": -0.3,
    **dict(zip((f"cm_ride:{n}" for n in range(RIDE_BUCKETS)), (0.0, 0.0, -1.0, -2.5, -5.0))),
    **dict(zip((f"cm_rain:{n}" for n in range(RAIN_BUCKETS)), (0.0, -0.1, -0.4, -1.0, -2.5))),
    "cm_rain_mm": -0.5,
}


def prior_mean() -> dict[str, float]:
    mean = dict.fromkeys(feature_names(), 0.0)
    for hour in range(24):
        # Start and end hour share the old day window between them, so a
        # slot entirely inside one hour band scores that band's full prior.
        mean[f"h_start:{hour}"] = hour_prior(hour) / 2
        mean[f"h_end:{hour}"] = hour_prior(hour) / 2
    for side in SIDES:
        for bucket, value in _GAP_PRIOR.items():
            mean[f"gap_{side}:{bucket}"] = value
    mean.update(_LATE_PRIOR)
    mean[htd_key("0-6")] = -1.0
    mean["days_from_now"] = -0.1
    for bucket, value in _LOAD_PRIOR.items():
        mean[load_key(bucket)] = value
    mean.update(_COMMUTE_PRIOR)
    return mean


# Prior standard deviation per feature group: how far one unit of evidence
# can move a weight, and how much the daily posterior draws vary while a
# group is unobserved. Season is kept tight on purpose: a year of data is
# needed before it means anything.
_GROUP_SIGMA = {
    "h_start": 1.0,
    "h_end": 1.0,
    "h_wt": 0.5,
    "h_eff": 0.5,
    "wd": 0.5,
    "wd_wt": 0.3,
    "season_sin": 0.05,
    "season_cos": 0.05,
    "days_from_now": 0.05,
    "gap_prev": 1.0,
    "gap_next": 1.0,
    "nb_prev": 0.5,
    "nb_next": 0.5,
    "late": 0.5,
    "late_h": 0.1,
    "htd": 0.5,
    "load": 0.5,
    "meet": 0.3,
    "cm_min": 0.01,
    "cm_transit": 0.3,
    "cm_ride": 0.7,
    "cm_rain": 0.7,
    "cm_rain_mm": 0.3,
    "cm_cold": 0.1,
    "cm_wind": 0.05,
    "cm_dark": 0.5,
    "cm_after_physical": 0.5,
    "bias": 3.0,
}


def feature_group(name: str) -> str:
    return name.split(":", 1)[0]


def prior_sigma(name: str) -> float:
    return _GROUP_SIGMA.get(feature_group(name), 0.5)
