"""Commute inside the solver: build the routing input for a solve and write
the solved legs back to the calendar.

Inputs: where the user and the bike are now (from the commute legs on the
calendar), the located fixed events and movable tasks of the horizon, a
travel matrix over their locations (bike and transit, from the route
cache), the hourly forecast, and the user's flips. Output: the legs of the
solved routes, written through the commute planner's writer so leg
identity, reminders and conflict alerts work exactly as before.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.commute.legs import HOME, PlannedLeg, _norm
from app.services.commute.state import CommuteState
from app.services.plan import features as F
from app.services.plan import solver as S
from app.services.plan.timeline import Grid, Item

log = logging.getLogger(__name__)

# Transit is time-dependent; the model books the slowest of these departures.
TRANSIT_REFERENCE_HOURS = (8, 13, 18)
LOOKUP_CONCURRENCY = 8


@dataclass
class RoutingContext:
    commute: S.CommuteInput
    locations: list[str]
    travel_seconds: dict[tuple[int, int, str], int]
    state: CommuteState
    calendar_events: list
    weather: dict[int, F.LegWeather] = field(default_factory=dict)


def routing_enabled() -> bool:
    settings = get_settings()
    return bool(
        getattr(settings, "commute_enabled", False)
        and (settings.home_address or "").strip()
        and settings.google_maps_api_key
        and (settings.google_calendar_id or "").strip()
    )


async def build(
    session: Session,
    *,
    grid: Grid,
    now: datetime,
    events: list[Item],
    fixed_tasks: dict,
    movable: list[tuple[str, object, object]],
    weights: dict[str, float],
) -> RoutingContext:
    """`movable` is `(solver key, task, attrs)` per movable task;
    `fixed_tasks` maps task id → its fixed `Item` (started or running)."""
    from app.db.clients import commute_log as commute_log_store
    from app.services.commute.state import load_state
    from app.services.location import resolve_location_alias, resolve_routable_location

    settings = get_settings()
    horizon_end = grid.at(grid.horizon)
    state, calendar_events = await load_state(session, now, horizon_end)
    home = settings.home_address.strip()
    locations = [home]
    index_of = {_norm(home): 0}
    resolved: dict[str, str] = {}

    async def index(raw: str | None) -> int | None:
        if not raw:
            return None
        if raw not in resolved:
            resolved[raw] = await resolve_routable_location(raw) or raw
        key = _norm(resolved[raw])
        if key not in index_of:
            index_of[key] = len(locations)
            locations.append(resolved[raw])
        return index_of[key]

    located_now = [
        item for item in [*events, *fixed_tasks.values()]
        if item.location and item.start <= now < item.end
    ]
    if located_now:
        here = max(located_now, key=lambda item: item.end)
        start_location = await index(here.location) or 0
        start_key = here.event_id or f"task:{here.task_id}"
        available = here.end
    else:
        start_location = await index(state.user_location) or 0
        start_key = state.user_anchor
        available = state.available_from or now
    bike_start = await index(state.bike_location) or 0

    # Hand-placed (locked) tasks are fixed for this solve, like events.
    fixed = await _route_nodes(grid, now, horizon_end, [*events, *fixed_tasks.values()], index)

    task_locations: dict[str, int] = {}
    task_anchors: dict[str, str] = {}
    task_physical: dict[str, bool] = {}
    for key, task, attrs in movable:
        location = resolve_location_alias(getattr(task, "location", None))
        if not location or _is_online(location):
            continue
        task_locations[key] = await index(location)
        task_anchors[key] = task.calendar_event_id or f"task:{task.id}"
        task_physical[key] = attrs.work_type == "physical" or attrs.effort == "high"

    travel_seconds = await _travel_matrix(locations, grid)
    travel: dict[tuple[int, int], tuple[int | None, int | None]] = {}
    leg_utility: dict[tuple[int, int, str], float] = {}
    for a in range(len(locations)):
        for b in range(len(locations)):
            if a == b:
                continue
            bike = travel_seconds.get((a, b, "bicycling"))
            transit = travel_seconds.get((a, b, "transit"))
            travel[(a, b)] = (_slots(bike), _slots(transit))
            for mode, seconds in (("bicycling", bike), ("transit", transit)):
                if seconds is None:
                    continue
                leg_utility[(a, b, mode)] = F.utility(F.leg_features(
                    mode, seconds / 60,
                    bike_max_minutes=settings.commute_bike_max_minutes,
                    rain_threshold_pct=settings.commute_rain_threshold_pct,
                ), weights)

    weather = await _weather(session, grid, now, home)
    ride_hour_utility = [
        F.utility(F.ride_weather_features(weather.get(h), settings.commute_rain_threshold_pct), weights)
        for h in range((grid.days + 1) * 24)
    ]

    commute = S.CommuteInput(
        travel=travel,
        leg_utility=leg_utility,
        ride_hour_utility=ride_hour_utility,
        after_physical_utility=weights.get("cm_after_physical", 0.0),
        fixed=fixed,
        task_locations=task_locations,
        task_anchors=task_anchors,
        task_physical=task_physical,
        start_location=start_location,
        start_key=start_key,
        available_from=grid.ceil(available),
        bike_start=bike_start,
        buffer=math.ceil(settings.commute_event_buffer_minutes / S.SLOT_MINUTES),
        home_stops=settings.commute_home_stops,
        candidate_days=settings.commute_candidate_days,
        pins=commute_log_store.active_pins(session, now),
        current={
            pair: (mode, grid.floor(depart)) for pair, (mode, depart) in state.upcoming.items()
        },
        now=grid.ceil(now),
    )
    return RoutingContext(
        commute=commute,
        locations=locations,
        travel_seconds=travel_seconds,
        state=state,
        calendar_events=calendar_events,
        weather=weather,
    )


def _is_online(location: str) -> bool:
    from app.services.location import is_online_location

    return is_online_location(location)


def _slots(seconds: int | None) -> int | None:
    if seconds is None:
        return None
    return max(1, math.ceil(seconds / 60 / S.SLOT_MINUTES))


async def _route_nodes(grid: Grid, now: datetime, horizon_end: datetime, events: list[Item], index):
    """Located fixed events ahead, overlapping ones merged into one node (the
    user is double-booked: entered once, left once)."""
    located = sorted(
        (e for e in events if e.location and now <= e.start < horizon_end),
        key=lambda e: e.start,
    )
    clusters: list[list[Item]] = []
    reach = None
    for item in located:
        if reach is not None and grid.floor(item.start) < reach:
            clusters[-1].append(item)
            reach = max(reach, grid.ceil(item.end))
        else:
            clusters.append([item])
            reach = grid.ceil(item.end)
    nodes = []
    for cluster in clusters:
        first = cluster[0]
        last = max(cluster, key=lambda e: e.end)
        nodes.append(S.RouteNode(
            key_in=first.event_id or first.ref,
            key_out=last.event_id or last.ref,
            start=grid.floor(first.start),
            end=grid.ceil(last.end),
            loc=await index(first.location),
            physical=any(e.work_type == "physical" or e.effort == "high" for e in cluster),
        ))
    return nodes


async def _travel_matrix(locations: list[str], grid: Grid) -> dict[tuple[int, int, str], int]:
    """Bike (time-invariant) and the slowest transit reference departure for
    every ordered location pair, through the route cache. Raises on a
    transient Maps failure: the caller then solves without commute."""
    from app.db import SessionLocal
    from app.services.commute.resolver import resolve_duration

    day = grid.origin + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    references = [datetime.combine(day, datetime.min.time(), tzinfo=grid.tz) + timedelta(hours=h)
                  for h in TRANSIT_REFERENCE_HOURS]
    gate = asyncio.Semaphore(LOOKUP_CONCURRENCY)

    async def lookup(origin: str, destination: str, mode: str, departure) -> int | None:
        async with gate:
            with SessionLocal() as session:
                return await resolve_duration(
                    session, origin=origin, destination=destination, mode=mode, departure=departure,
                )

    jobs = []
    keys = []
    for a, origin in enumerate(locations):
        for b, destination in enumerate(locations):
            if a == b:
                continue
            jobs.append(lookup(origin, destination, "bicycling", None))
            keys.append((a, b, "bicycling", 0))
            for n, ref in enumerate(references):
                jobs.append(lookup(origin, destination, "transit", ref))
                keys.append((a, b, "transit", n))
    results = await asyncio.gather(*jobs)
    out: dict[tuple[int, int, str], int] = {}
    for (a, b, mode, _), seconds in zip(keys, results):
        if seconds is None:
            continue
        out[(a, b, mode)] = max(out.get((a, b, mode), 0), seconds)
    return out


async def _weather(session: Session, grid: Grid, now: datetime, home: str) -> dict[int, F.LegWeather]:
    from app.services.commute.weather import blended_rain_pct, geocode, hourly_forecast

    try:
        latlon = await geocode(session, home)
    except Exception as exc:  # noqa: BLE001
        log.info("routing · home geocode failed err=%s", exc)
        return {}
    if latlon is None:
        return {}
    hours = (grid.days + 1) * 24
    forecast = await hourly_forecast(latlon[0], latlon[1], grid.at(0), grid.at(hours * S.SLOTS_PER_HOUR))
    out: dict[int, F.LegWeather] = {}
    for h in range(hours):
        when = grid.at(h * S.SLOTS_PER_HOUR)
        row = forecast.get(when.strftime("%Y-%m-%dT%H:00"))
        if not row:
            continue
        lead = (when - now).total_seconds() / 3600
        out[h] = F.LegWeather(
            rain_pct=blended_rain_pct(row.get("rain_pct", 0.0), lead),
            rain_mm=row.get("rain_mm", 0.0),
            temp_c=row.get("temp_c"),
            wind_kmh=row.get("wind_kmh"),
            dark=row.get("is_day", 1.0) == 0,
        )
    return out


RAIN_CHANGE_HOURS = 48
RAIN_CHANGE_POINTS = 10


def rain_snapshot(ctx: RoutingContext, grid: Grid, now: datetime) -> dict[str, float]:
    """Rain probabilities for the next 48 h by local hour, to tell a material
    forecast change from noise."""
    first = grid.floor(now) // S.SLOTS_PER_HOUR
    return {
        grid.at(h * S.SLOTS_PER_HOUR).strftime("%Y-%m-%dT%H:00"): w.rain_pct
        for h, w in ctx.weather.items()
        if first <= h < first + RAIN_CHANGE_HOURS
    }


def rain_changed(before: dict[str, float], after: dict[str, float]) -> bool:
    return any(
        abs(after[hour] - before[hour]) >= RAIN_CHANGE_POINTS for hour in after.keys() & before.keys()
    )


# --- Writing legs ------------------------------------------------------------------


def planned_legs(
    ctx: RoutingContext,
    routes: list[S.RouteLeg],
    *,
    grid: Grid,
    anchors: dict[str, str | None],
) -> list[PlannedLeg]:
    """Solved routes → calendar legs. `anchors` maps the solver's provisional
    `task:<id>` keys to the calendar event ids written for them."""
    settings = get_settings()
    buffer = timedelta(minutes=S.SLOT_MINUTES * ctx.commute.buffer)
    out: list[PlannedLeg] = []
    for leg in routes:
        origin_anchor = anchors.get(leg.origin_key, leg.origin_key)
        dest_anchor = anchors.get(leg.dest_key, leg.dest_key)
        if not origin_anchor or not dest_anchor:
            continue
        seconds = ctx.travel_seconds.get((leg.origin_loc, leg.dest_loc, leg.mode))
        travel = timedelta(seconds=seconds) if seconds else timedelta(
            minutes=leg.travel * S.SLOT_MINUTES
        )
        if leg.shortfall:
            arrive = grid.at(leg.end) - buffer
            depart = arrive - travel
        else:
            depart = grid.at(leg.start) + buffer
            arrive = depart + travel
        out.append(PlannedLeg(
            origin_anchor=origin_anchor,
            dest_anchor=dest_anchor,
            origin=ctx.locations[leg.origin_loc],
            destination=ctx.locations[leg.dest_loc],
            mode=leg.mode,
            depart=depart,
            arrive=arrive,
            reason=_reason(ctx, leg, settings),
            note=_note(ctx, leg),
        ))
    return out


def _short(address: str) -> str:
    return address.split(",")[0].strip() or address


def _reason(ctx: RoutingContext, leg: S.RouteLeg, settings) -> str | None:
    if leg.pinned:
        return "switched by you"
    if leg.mode != "transit":
        return None
    bike = ctx.travel_seconds.get((leg.origin_loc, leg.dest_loc, "bicycling"))
    if bike is None:
        return "no bike route"
    if leg.bike_before != leg.origin_loc:
        return "bike is at home" if leg.bike_before == 0 else (
            f"bike is at {_short(ctx.locations[leg.bike_before])}"
        )
    hour = leg.start // S.SLOTS_PER_HOUR
    weather = ctx.weather.get(hour)
    threshold = settings.commute_rain_threshold_pct
    if weather is not None and F.rain_bucket(weather.rain_pct, threshold) >= 3:
        return f"rain {weather.rain_pct:.0f} % at {hour % 24:02d}:00"
    if bike / 60 > settings.commute_bike_max_minutes:
        return f"{round(bike / 60)}-min ride"
    return "quicker overall"


def _note(ctx: RoutingContext, leg: S.RouteLeg) -> str | None:
    if leg.mode == "bicycling" and leg.origin_loc != 0 and leg.bike_before == leg.origin_loc:
        place = _short(ctx.locations[leg.origin_loc])
        since = ctx.state.bike_since
        if since is not None and leg.bike_before == ctx.commute.bike_start:
            return f"Bike is at {place} since {since.strftime('%a')} — riding it back"
        return f"Bike is at {place} — riding it back"
    if leg.mode != "bicycling" and leg.dest_loc == 0 and leg.bike_after != 0:
        return f"Bike stays at {_short(ctx.locations[leg.bike_after])}"
    return None


async def write_legs(
    session: Session,
    ctx: RoutingContext,
    legs: list[PlannedLeg],
    *,
    now: datetime,
    horizon_end: datetime,
    obstacles: list[tuple[str, str, datetime, datetime]],
) -> dict:
    """Diff the solved legs against the not-yet-started commute events and
    write them; started legs are history and stay untouched."""
    from app.services.calendar import is_commute_event
    from app.services.commute import planner

    settings = get_settings()
    calendar_id = settings.google_calendar_id.strip()
    write_events = [ev for ev in ctx.calendar_events if ev.calendar_id == calendar_id]
    existing = [ev for ev in write_events if is_commute_event(ev) and ev.start > now]
    conflicts = planner._immovable_conflicts(legs, obstacles)
    written, failures, new_conflicts = await planner._write_legs(
        session, legs, calendar_id=calendar_id, existing=existing,
        window=(now, horizon_end), account_key=None, conflicts=conflicts,
    )
    await planner._notify_new_failures(failures)
    await planner._notify_new_conflicts(new_conflicts)
    await planner._sync_anchor_reminders(
        session, events=write_events, legs=legs, window=(now, horizon_end),
        calendar_id=calendar_id, settings=settings, account_key=None,
    )
    return {"written": written, "conflicts": len(conflicts), "failures": len(failures)}


__all__ = ["HOME", "RoutingContext", "build", "planned_legs", "rain_snapshot", "routing_enabled", "write_legs"]
