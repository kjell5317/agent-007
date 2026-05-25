"""Week-ahead commute planner.

For every event with a physical location in the next `commute_lookahead_days`,
decides:

  * **Mode** — bike by default; switch to public transport when the bike trip
    exceeds `commute_bike_max_minutes` OR when rain probability at the
    departure hour is at/above `commute_rain_threshold_pct`.

  * **Origin of the outbound leg** —
      - If the *previous* event ended close enough that we'd reach the next
        one from there before its start, we route `prev → curr`.
      - But if we could detour through home (curr_start − prev_end ≥
        `commute_home_layover_minutes` + travel time both legs), we go home.
      - Otherwise (no previous event today, or it ended at home), we start
        from `home_address`.

  * **Online events** — never queue a commute themselves, but they *do* shift
    the inbound (home) leg: if a physical event ends and an online starts
    before there's time to commute home, the inbound leg slides past the
    online's end. Chained online events repeat the same check.

The planner then writes `🚲 Commute` / `🚆 Commute` events back to the
calendar (idempotent — re-running replaces in place) and reschedules any task
event that now overlaps with a commute window via
[reschedule_for_commute][app.services.commute.reschedule.reschedule_overlapping_tasks].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.commute.resolver import resolve_duration
from app.services.commute.weather import geocode, precipitation_probability_at
from app.services.google_calendar import (
    WINDOW_DAYS,
    CalendarEvent,
    create_event,
    delete_event,
    list_week_events,
)

log = logging.getLogger(__name__)

COMMUTE_TAG = "[commute]"


@dataclass
class CommutePlan:
    leg: str  # "outbound" or "inbound"
    origin: str
    destination: str
    mode: str  # "bicycling" | "transit"
    depart: datetime
    arrive: datetime
    related_event_id: str
    # Why this mode was chosen — surfaced in the calendar event description so
    # you can tell at a glance whether the planner switched to transit because
    # of rain, a too-long bike trip, or just because biking wasn't routable.
    # `None` means "no reason to log" (default: bike fits, no rain).
    mode_reason: str | None = None
    rain_pct: int | None = None
    bike_minutes: int | None = None


async def plan_week_commutes(
    session: Session,
    *,
    week_start: datetime | None = None,
    account_key: str | None = None,
) -> dict:
    """Plan commutes for every event in the next `commute_lookahead_days` days.

    Returns a summary dict: `{planned, skipped_online, skipped_no_location,
    rescheduled_tasks, errors}`.
    """
    settings = get_settings()
    if not settings.home_address:
        log.info("commute planner skipped — HOME_ADDRESS not set")
        return _empty_summary("home_address not configured")
    if not settings.google_maps_api_key:
        log.info("commute planner skipped — GOOGLE_MAPS_API_KEY not set")
        return _empty_summary("google_maps_api_key not configured")
    write_calendar_id = (settings.google_calendar_id or "").strip()
    if not write_calendar_id:
        return _empty_summary("google_calendar_id not configured")

    # Read from every busy calendar so commutes account for events on shared /
    # secondary calendars; write to the primary `google_calendar_id` only.
    read_calendar_ids = _read_calendar_ids(settings, write_calendar_id)

    anchor = (week_start or datetime.now(timezone.utc)).astimezone()
    events = await list_week_events(
        session,
        calendar_ids=read_calendar_ids,
        week_start=anchor,
        account_key=account_key,
    )
    physical, online, existing_commutes = _partition(events, write_calendar_id)
    home = settings.home_address
    home_latlon = await geocode(home)

    # NOT_FOUND / ZERO_RESULTS from Maps is a stable signal that the address
    # can't be reached. Such events are folded straight into `online` — same
    # treatment everywhere downstream: they occupy the user but don't trigger
    # a commute. The count is reported separately for diagnostics only.
    physical, unroutable = await _filter_routable(session, physical, home)
    online = sorted([*online, *unroutable], key=lambda e: e.start)
    log.info(
        "commute planner · physical=%d online=%d (of which unroutable=%d) "
        "existing_commute=%d window=%dd",
        len(physical), len(online), len(unroutable), len(existing_commutes), WINDOW_DAYS,
    )

    summary = _empty_summary()
    summary["skipped_online"] = len(online)
    summary["skipped_unroutable"] = len(unroutable)

    # Online events occupy the user even though they don't trigger a commute;
    # we need a chronological view to know when the user is actually free to
    # head home after a physical event.
    occupied = sorted([*physical, *online], key=lambda e: e.start)
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)

    plans: list[CommutePlan] = []
    for idx, ev in enumerate(physical):
        # `_partition` only keeps events with a non-empty location, so this is
        # always a string by the time we get here.
        assert ev.location is not None
        prev = physical[idx - 1] if idx > 0 else None
        outbound_origin = _outbound_origin(home, prev, ev, settings)
        plan = await _build_leg(
            session,
            origin=outbound_origin,
            destination=ev.location,
            arrive_by=ev.start - buffer,
            home_latlon=home_latlon,
            settings=settings,
            related_event_id=ev.id,
            leg="outbound",
        )
        if plan is not None:
            plans.append(plan)

        next_ev = physical[idx + 1] if idx + 1 < len(physical) else None
        next_physical_start = next_ev.start if next_ev is not None else None
        inbound_depart = _effective_inbound_depart(
            ev, occupied, next_physical_start, settings,
        )
        if _should_return_home(inbound_depart, next_ev, settings):
            plan = await _build_leg(
                session,
                origin=ev.location,
                destination=home,
                arrive_by=None,
                depart_at=inbound_depart,
                home_latlon=home_latlon,
                settings=settings,
                related_event_id=ev.id,
                leg="inbound",
            )
            if plan is not None:
                plans.append(plan)

    summary["planned"] = await _write_plans(
        session, plans, write_calendar_id, existing_commutes, account_key,
    )

    # Lazy import to keep the planner module loadable without sync.py side effects.
    from app.services.commute.reschedule import reschedule_overlapping_tasks

    summary["rescheduled_tasks"] = await reschedule_overlapping_tasks(
        session, plans, account_key=account_key,
    )
    return summary


def _empty_summary(reason: str | None = None) -> dict:
    out: dict = {
        "planned": 0,
        "skipped_online": 0,
        "skipped_unroutable": 0,
        "skipped_no_location": 0,
        "rescheduled_tasks": 0,
        "errors": [],
    }
    if reason:
        out["errors"].append({"setup": reason})
    return out


async def _filter_routable(
    session: Session,
    physical: list[CalendarEvent],
    home: str,
) -> tuple[list[CalendarEvent], list[CalendarEvent]]:
    """Split `physical` into (routable, unroutable) from home.

    A destination is unroutable when neither bike nor transit returns a
    duration. We probe from `home` only — if the user can't be routed there
    from their own home, there's no realistic origin we'd succeed from. The
    resolver caches negative answers so this pre-pass costs at most one bike
    + one transit call per *distinct* destination, lifetime.
    """
    routable: list[CalendarEvent] = []
    unroutable: list[CalendarEvent] = []
    for ev in physical:
        assert ev.location is not None
        # Departure-time hint for the transit lookup; the bike call ignores it.
        departure_hint = ev.start - timedelta(hours=1)
        bike = await resolve_duration(
            session,
            origin=home,
            destination=ev.location,
            mode="bicycling",
            departure=departure_hint,
        )
        if bike is not None:
            routable.append(ev)
            continue
        transit = await resolve_duration(
            session,
            origin=home,
            destination=ev.location,
            mode="transit",
            departure=departure_hint,
        )
        if transit is not None:
            routable.append(ev)
            continue
        log.info(
            "commute · unroutable destination, treating as online: event=%s loc=%r",
            ev.id, ev.location,
        )
        unroutable.append(ev)
    return routable, unroutable


def _read_calendar_ids(settings, write_calendar_id: str) -> list[str]:
    """Union of `google_calendar_id` and `google_busy_calendar_ids`, in order,
    de-duplicated. Read-side: every calendar that should influence the plan."""
    seen: set[str] = set()
    out: list[str] = []
    for cid in [write_calendar_id, *settings.google_busy_calendar_ids]:
        clean = (cid or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _partition(
    events: list[CalendarEvent],
    write_calendar_id: str,
) -> tuple[list[CalendarEvent], list[CalendarEvent], list[CalendarEvent]]:
    """Split events into (physical, online, existing_commute) lists.

    Only events on the write calendar can be `existing_commute` — a tagged
    event on a shared/secondary calendar would be re-deleted on every run,
    which we don't own and shouldn't touch.
    """
    physical: list[CalendarEvent] = []
    online: list[CalendarEvent] = []
    commutes: list[CalendarEvent] = []
    for ev in events:
        if ev.all_day:
            continue
        is_commute_tag = (ev.description or "").strip().startswith(COMMUTE_TAG)
        if is_commute_tag and ev.calendar_id == write_calendar_id:
            commutes.append(ev)
            continue
        if is_commute_tag:
            continue
        if _is_online(ev):
            online.append(ev)
            continue
        if not (ev.location or "").strip():
            continue
        physical.append(ev)
    return physical, online, commutes


def _is_online(ev: CalendarEvent) -> bool:
    """Online = no physical location.

    Google Meet / Zoom typically populate `hangoutLink` or a video URL in
    `conferenceData` even without a `location`. We treat the *absence* of a
    `location` string as the signal: if the user typed an address, we plan a
    commute regardless of whether there's also a video link.
    """
    loc = (ev.location or "").strip()
    if not loc:
        return True
    lowered = loc.lower()
    return lowered.startswith(("http://", "https://")) or "zoom.us" in lowered


def _outbound_origin(
    home: str,
    prev: CalendarEvent | None,
    curr: CalendarEvent,
    settings,
) -> str:
    """Pick where the outbound leg starts.

    Rule from the user: if `event → home → event` fits with at least
    `commute_home_layover_minutes` of buffer at home, go home in between.
    We *don't* know the actual transit time yet — but we can lower-bound the
    detour as `2 * (curr_start - prev_end) - layover ≥ 0`. That's enough to
    rule out trips where the previous event ended 10 minutes ago.
    """
    if prev is None:
        return home
    gap = curr.start - prev.end
    layover = timedelta(minutes=settings.commute_home_layover_minutes)
    # Generous upper bound on each leg: half the gap, minus the layover.
    if gap >= 2 * layover:
        return home
    return prev.location or home


def _should_return_home(
    depart: datetime,
    next_ev: CalendarEvent | None,
    settings,
) -> bool:
    """Return-home decision given the time the user is actually free to leave.

    - Last event of the planning window → always go home.
    - Otherwise, return home only if there's enough gap between `depart` and
      the next physical event to fit a layover at home.
    """
    if next_ev is None:
        return True
    gap = next_ev.start - depart
    return gap >= 2 * timedelta(minutes=settings.commute_home_layover_minutes)


def _effective_inbound_depart(
    ev: CalendarEvent,
    occupied: list[CalendarEvent],
    next_physical_start: datetime | None,
    settings,
) -> datetime:
    """When the user is actually free to commute home after `ev`.

    Starts from `ev.end + commute_event_buffer_minutes` (the user lingers a few
    minutes after the meeting). If an online event starts before the user
    could realistically commute home (less than `commute_bike_max_minutes`
    between the current departure and the online's start), the user attends
    the online from wherever they are; departure is pushed past its end.
    Chains of online events repeat the same check. We never push past
    `next_physical_start` — anything beyond that is a different physical
    event's commute to handle.
    """
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    depart = ev.end + buffer
    threshold = timedelta(minutes=settings.commute_bike_max_minutes)
    for other in occupied:
        if other.id == ev.id:
            continue
        if other.end <= depart:
            continue
        # `next_physical_start` bounds this window — anything at/after it is
        # the next physical event's problem to handle. Combined with skipping
        # `ev` itself, what remains in the window is exactly the non-commute
        # events that occupy the user (online + unroutable).
        if next_physical_start is not None and other.start >= next_physical_start:
            break
        if other.start - depart < threshold:
            # `+ buffer` keeps the contract consistent: every commute home
            # departs `buffer` minutes after the *last* thing that held the
            # user, not flush against its end.
            depart = max(depart, other.end + buffer)
    return depart


async def _build_leg(
    session: Session,
    *,
    origin: str,
    destination: str,
    arrive_by: datetime | None,
    depart_at: datetime | None = None,
    home_latlon: tuple[float, float] | None,
    settings,
    related_event_id: str,
    leg: str,
) -> CommutePlan | None:
    """Decide mode + timing for a single leg.

    Either `arrive_by` (outbound — we need to be there by event start) or
    `depart_at` (inbound — we leave when the event ends) is set, never both.
    """
    if origin == destination:
        return None

    # Reference time used both for the maps lookup (departure hour-bucket) and
    # for the rain check. For outbound legs we approximate departure as "an
    # hour before arrival" since we don't know the exact duration yet; the
    # resolver only cares about the hour-of-week bucket so the approximation
    # is fine.
    reference = depart_at if depart_at is not None else arrive_by - timedelta(hours=1)

    bike_seconds = await resolve_duration(
        session,
        origin=origin,
        destination=destination,
        mode="bicycling",
        departure=reference,
    )

    rain_pct: int | None = None
    if home_latlon is not None:
        when_for_rain = arrive_by or depart_at
        rain_pct = await precipitation_probability_at(
            home_latlon[0], home_latlon[1], when_for_rain,
        )

    bike_minutes: int | None = (
        round(bike_seconds / 60) if bike_seconds is not None else None
    )

    use_transit = False
    reason: str | None = None
    if bike_seconds is None:
        use_transit = True
        reason = "bike route unavailable"
    elif bike_minutes is not None and bike_minutes > settings.commute_bike_max_minutes:
        use_transit = True
        reason = (
            f"bike {bike_minutes}min > {settings.commute_bike_max_minutes}min threshold"
        )
    elif rain_pct is not None and rain_pct >= settings.commute_rain_threshold_pct:
        use_transit = True
        reason = (
            f"rain {rain_pct}% ≥ {settings.commute_rain_threshold_pct}% threshold"
        )

    mode: str
    duration_s: int | None
    if use_transit:
        # The transit-departure timestamp matters here — pass a real one so
        # Google snaps to the right schedule.
        departure_for_transit = depart_at or (arrive_by - timedelta(hours=1))
        duration_s = await resolve_duration(
            session,
            origin=origin,
            destination=destination,
            mode="transit",
            departure=departure_for_transit,
        )
        mode = "transit"
        if duration_s is None:
            # Transit returned no result (rural, off-hours, …). Fall back to
            # the bike duration if we have one.
            if bike_seconds is None:
                log.info(
                    "commute · no route found %s -> %s; skipping",
                    origin, destination,
                )
                return None
            mode = "bicycling"
            duration_s = bike_seconds
            reason = (
                f"{reason}; transit unavailable, fell back to bike"
                if reason
                else "transit unavailable, fell back to bike"
            )
    else:
        mode = "bicycling"
        duration_s = bike_seconds

    travel = timedelta(seconds=duration_s)
    if arrive_by is not None:
        depart = arrive_by - travel
        arrive = arrive_by
    else:
        depart = depart_at  # type: ignore[assignment]
        arrive = depart_at + travel  # type: ignore[operator]

    return CommutePlan(
        leg=leg,
        origin=origin,
        destination=destination,
        mode=mode,
        depart=depart,
        arrive=arrive,
        related_event_id=related_event_id,
        mode_reason=reason,
        rain_pct=rain_pct,
        bike_minutes=bike_minutes,
    )


async def _write_plans(
    session: Session,
    plans: list[CommutePlan],
    calendar_id: str,
    existing_commutes: list[CalendarEvent],
    account_key: str | None,
) -> int:
    """Persist plans as calendar events. Existing commute events are dropped
    first so the result is idempotent — re-running with the same input gives
    the same calendar."""
    for stale in existing_commutes:
        try:
            await delete_event(
                session,
                calendar_id=calendar_id,
                event_id=stale.id,
                account_key=account_key,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.warning("commute · stale event %s delete failed: %s", stale.id, exc)

    written = 0
    for plan in plans:
        summary = _summary_for(plan)
        description = _description_for(plan)
        try:
            await create_event(
                session,
                calendar_id=calendar_id,
                summary=summary,
                start=plan.depart,
                end=plan.arrive,
                description=description,
                location=plan.destination,
                account_key=account_key,
            )
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · create_event failed leg=%s err=%s", plan.leg, exc,
            )
    return written


def _summary_for(plan: CommutePlan) -> str:
    icon = "🚲" if plan.mode == "bicycling" else "🚆"
    return f"{icon} To {plan.destination.split(',')[0]}"


def _description_for(plan: CommutePlan) -> str:
    lines = [
        COMMUTE_TAG,
        f"From: {plan.origin}",
        f"To: {plan.destination}",
        f"Mode: {plan.mode}",
    ]
    if plan.mode_reason:
        lines.append(f"Reason: {plan.mode_reason}")
    return "\n".join(lines)
