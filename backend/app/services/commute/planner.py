"""Commute planning over a calendar window.

Builds the anchor timeline (physical events + scheduled located tasks —
both are just calendar events with a location), derives the desired legs
via `app.services.commute.legs`, and diffs them against the managed
commute events on the write calendar. Legs are identified by
`(origin_anchor, dest_anchor)`, so a moved anchor's legs are patched in
place rather than deleted and recreated.

Callers must hold the plan-service scheduling lock (see
`app.services.plan.commute`, which wraps every entry point) — the diff
reads a live calendar snapshot, and the follow-up task reschedules write
through the same planner state.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.calendar import (
    SILENT_REMINDERS,
    CalendarEvent,
    commute_leg_key,
    commute_private_properties,
    create_event,
    delete_event,
    is_commute_event,
    is_task_event,
    list_events_between,
    patch_event,
    popup_reminders,
    private_properties,
    reminders_differ,
)
from app.services.commute.legs import (
    FAILED_MODE,
    HOME,
    Anchor,
    Durations,
    PlannedLeg,
    derive_legs,
    required_routes,
)
from app.services.commute.resolver import resolve_duration
from app.services.commute.weather import geocode, precipitation_probabilities_between
from app.services.location import (
    is_online_location,
    resolve_location_alias,
    resolve_tum_room,
)
from app.timezones import to_user_tz

log = logging.getLogger(__name__)


async def refresh_weather_sensitive_commutes(
    session: Session,
    *,
    account_key: str | None = None,
    _depth: int = 0,
) -> dict:
    """Re-derive commute legs for the next day when rain would flip a
    bike leg to transit."""
    settings = get_settings()
    summary = _empty_summary()
    write_calendar_id = (settings.google_calendar_id or "").strip()
    if not write_calendar_id:
        summary["errors"].append({"setup": "google_calendar_id not configured"})
        return summary
    if not settings.home_address:
        summary["errors"].append({"setup": "home_address not configured"})
        return summary
    if not settings.google_maps_api_key:
        # Without the key `geocode` returns None before any HTTP call, which
        # would be misreported below as an un-geocodable address.
        summary["errors"].append({"setup": "google_maps_api_key not configured"})
        return summary

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=1)
    home_latlon = await geocode(session, settings.home_address)
    if home_latlon is None:
        summary["errors"].append({"setup": "home address could not be geocoded"})
        return summary

    hourly_rain = await precipitation_probabilities_between(
        home_latlon[0],
        home_latlon[1],
        now,
        window_end,
    )
    rain_pct = max(hourly_rain.values()) if hourly_rain else None
    if rain_pct is None or rain_pct < settings.commute_rain_threshold_pct:
        log.info(
            "commute weather refresh skipped · rain=%s threshold=%s",
            rain_pct,
            settings.commute_rain_threshold_pct,
        )
        return summary

    events = await list_events_between(
        session,
        calendar_ids=[write_calendar_id],
        time_min=now,
        time_max=window_end,
        account_key=account_key,
    )
    if not any(is_commute_event(ev) and _commute_mode(ev) == "bicycling" for ev in events):
        return summary

    return await plan_window_commutes(
        session,
        window_start=now,
        window_end=window_end,
        hourly_rain=hourly_rain,
        account_key=account_key,
        _depth=_depth,
    )


async def plan_window_commutes(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    hourly_rain: dict[str, int] | None = None,
    account_key: str | None = None,
    _depth: int = 0,
) -> dict:
    settings = get_settings()
    summary = _empty_summary()
    write_calendar_id = (settings.google_calendar_id or "").strip()
    if not write_calendar_id:
        summary["errors"].append({"setup": "google_calendar_id not configured"})
        return summary
    home = (settings.home_address or "").strip()
    if not home:
        summary["errors"].append({"setup": "home_address not configured"})
        return summary
    if not settings.google_maps_api_key:
        summary["errors"].append({"setup": "google_maps_api_key not configured"})
        return summary

    # Legs in the past can't be ridden, and every anchor pair in the window
    # costs routing quota — so a stale caller window (e.g. re-placing a
    # long-overdue task whose prior slot is months back) must not fan out
    # over historical events. Clamp to now and cap the width at the
    # configured lookahead.
    now = datetime.now(timezone.utc)
    window_start = max(window_start, now)
    max_end = window_start + timedelta(days=settings.commute_lookahead_days)
    if window_end > max_end:
        log.warning(
            "commute · window capped at %d days (requested until %s)",
            settings.commute_lookahead_days, window_end.isoformat(),
        )
        window_end = max_end
    if window_end <= window_start:
        return summary

    window_start = to_user_tz(window_start)
    window_end = to_user_tz(window_end)
    margin = _context_margin(settings)
    events = await list_events_between(
        session,
        calendar_ids=_read_calendar_ids(settings, write_calendar_id),
        time_min=window_start - margin,
        time_max=window_end + margin,
        account_key=account_key,
    )
    anchors, existing_commutes, skipped_online = _partition(events, write_calendar_id)
    anchors = await _resolve_tum_anchors(anchors)
    summary["skipped_online"] = skipped_online

    if hourly_rain is None:
        hourly_rain = await _home_rain(session, home, window_start, window_end + margin)

    durations = await _resolve_routes(
        session, anchors, home, hourly_rain, settings, summary,
    )
    if durations is None:
        # Transient Maps failure — anything derived now would turn every leg
        # into a bogus "no route" placeholder. Leave the calendar untouched;
        # the next scheduled pass retries.
        return summary
    legs, skipped_unroutable = derive_legs(anchors, home, durations, hourly_rain, settings)
    summary["skipped_unroutable"] = skipped_unroutable

    # Only legs touching the requested window are written or deleted — the
    # margin exists so edge anchors see their real neighbours, not to widen
    # the replan zone.
    to_write = [
        leg for leg in legs if _overlaps(leg.depart, leg.arrive, window_start, window_end)
    ]

    # A task anchor whose legs collide with another anchor has a slot with
    # no room for its trip (typically placed before trip-block planning
    # existed). Re-place the task instead of writing overlapping legs — its
    # own replan then derives clean ones.
    task_event_ids = {ev.id for ev in events if is_task_event(ev)}
    needs_replacement = _reschedule_candidates(to_write, anchors, task_event_ids)
    if needs_replacement:
        to_write = [leg for leg in to_write if not (set(leg.key) & needs_replacement)]

    summary["planned"], new_failures = await _write_legs(
        session,
        to_write,
        calendar_id=write_calendar_id,
        existing=existing_commutes,
        window=(window_start, window_end),
        account_key=account_key,
    )
    await _notify_new_failures(new_failures)

    # Arrival truth comes from the full derived set — an anchor at the window
    # edge may keep a leg that was written by an earlier replan.
    await _sync_anchor_reminders(
        session,
        events=events,
        legs=legs,
        window=(window_start, window_end),
        calendar_id=write_calendar_id,
        settings=settings,
        account_key=account_key,
    )

    from app.services.commute.reschedule import reschedule_overlapping_tasks

    summary["rescheduled_tasks"] = await reschedule_overlapping_tasks(
        session,
        to_write,
        account_key=account_key,
        _depth=_depth,
    )
    summary["rescheduled_tasks"] += await _reschedule_task_anchors(
        session,
        needs_replacement,
        account_key=account_key,
        _depth=_depth,
    )
    return summary


# Marks a foreign (user-created) event whose reminders we silenced because an
# arriving leg carries the notification — so we know to restore the calendar
# default when the leg goes away, and never touch events the user silenced
# themselves.
_REMINDERS_MANAGED_PROP = "reminders_managed"


def _reminders_for_leg(leg: PlannedLeg) -> dict:
    """Legs arriving at an anchor carry the pre-event popup; rides home stay
    silent (explicitly, so calendar defaults don't fire on them)."""
    if leg.dest_anchor == HOME:
        return SILENT_REMINDERS
    return popup_reminders(get_settings().reminder_lead_minutes)


def _desired_anchor_reminders(
    ev: CalendarEvent,
    has_arriving_leg: bool,
    settings,
) -> tuple[dict | None, dict | None]:
    """`(reminders, private_property_patch)` an anchor should get, or
    `(None, None)` to leave it alone.

    Task events are fully managed: silent behind an arriving leg, popup
    otherwise. Foreign events are only silenced when a leg takes over —
    marked so the calendar default comes back when the leg disappears —
    and never touched if the user silenced them independently."""
    if is_task_event(ev):
        desired = SILENT_REMINDERS if has_arriving_leg else popup_reminders(settings.reminder_lead_minutes)
        return desired, None

    managed = private_properties(ev).get(_REMINDERS_MANAGED_PROP) == "true"
    if has_arriving_leg:
        if managed:
            return SILENT_REMINDERS, None
        return SILENT_REMINDERS, {_REMINDERS_MANAGED_PROP: "true"}
    if managed:
        return {"useDefault": True}, {_REMINDERS_MANAGED_PROP: ""}
    return None, None


async def _sync_anchor_reminders(
    session: Session,
    *,
    events: list[CalendarEvent],
    legs: list[PlannedLeg],
    window: tuple[datetime, datetime],
    calendar_id: str,
    settings,
    account_key: str | None,
) -> None:
    """Move the pre-start popup onto the arriving leg (and back) for every
    anchor on the write calendar inside the replanned window."""
    arriving = {leg.dest_anchor for leg in legs if leg.dest_anchor != HOME}
    for ev in events:
        if ev.calendar_id != calendar_id or ev.all_day or is_commute_event(ev):
            continue
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        desired, prop_patch = _desired_anchor_reminders(ev, ev.id in arriving, settings)
        if desired is None:
            continue
        differ = reminders_differ(ev, desired)
        claiming = prop_patch is not None and prop_patch.get(_REMINDERS_MANAGED_PROP) == "true"
        if claiming and not differ:
            # Already silent by the user's own hand — don't claim it, or the
            # leg's later removal would "restore" defaults they never wanted.
            continue
        if not differ and prop_patch is None:
            continue
        try:
            await patch_event(
                session,
                calendar_id=calendar_id,
                event_id=ev.id,
                reminders=desired,
                private_properties=prop_patch,
                account_key=account_key,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · reminder sync failed event=%s err=%s", ev.id, exc)


def _reschedule_candidates(
    legs: list[PlannedLeg],
    anchors: list[Anchor],
    task_event_ids: set[str],
) -> set[str]:
    """Task anchors whose derived legs overlap a *different* anchor. Fixed
    events can't move, so a fixed↔fixed collision is left alone (and
    logged by the write path as an overlapping leg)."""
    out: set[str] = set()
    for leg in legs:
        for anchor in anchors:
            if anchor.id in leg.key:
                continue
            if leg.depart < anchor.end and anchor.start < leg.arrive:
                out.update(anchor_id for anchor_id in leg.key if anchor_id in task_event_ids)
                break
    return out


async def _reschedule_task_anchors(
    session: Session,
    event_ids: set[str],
    *,
    account_key: str | None,
    _depth: int,
) -> int:
    if not event_ids:
        return 0
    from sqlalchemy import select

    from app.db.models.task import Task
    from app.services.plan.schedule import schedule_task

    stmt = select(Task).where(Task.calendar_event_id.in_(list(event_ids)))
    moved = 0
    for task in session.execute(stmt).scalars():
        log.info("commute · task=%s slot has no room for its trip; re-placing", task.id)
        result = await schedule_task(session, task, account_key=account_key, _depth=_depth)
        if result is not None:
            moved += 1
    return moved


async def _resolve_routes(
    session: Session,
    anchors: list[Anchor],
    home: str,
    hourly_rain: dict[str, int] | None,
    settings,
    summary: dict,
) -> Durations | None:
    """Route durations for every anchor pair, or None when a transient Maps
    failure aborted resolution. `resolve_duration` answers definitive
    no-route lookups with None values; it only *raises* on transient
    failures (quota, network) — and those would hit every remaining pair
    identically, so bail on the first instead of burning a quota-priced
    call per pair."""
    max_rain = max(hourly_rain.values()) if hourly_rain else None
    rain_possible = max_rain is not None and max_rain >= settings.commute_rain_threshold_pct
    home_norm = " ".join(home.lower().split())
    durations: Durations = {}
    resolved = 0
    for (origin, destination), reference in required_routes(anchors, home).items():
        try:
            bike = await resolve_duration(
                session, origin=origin, destination=destination,
                mode="bicycling", departure=reference,
            )
            durations[(origin, destination, "bicycling")] = bike
            resolved += 1
            # Legs not starting at home may be forced onto transit by the
            # bike-stays-home chain rule, so their transit duration must be
            # on hand even when the bike would otherwise win.
            mid_chain = " ".join(origin.lower().split()) != home_norm
            if (
                bike is None
                or bike > settings.commute_bike_max_minutes * 60
                or rain_possible
                or mid_chain
            ):
                durations[(origin, destination, "transit")] = await resolve_duration(
                    session, origin=origin, destination=destination,
                    mode="transit", departure=reference,
                )
                resolved += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · route resolution aborted at %s -> %s err=%s",
                origin, destination, exc,
            )
            summary["errors"].append({
                "route": f"{origin} -> {destination}",
                "error": str(exc),
                "transient": True,
            })
            return None
    if resolved:
        log.info("commute · resolved %d route durations for %d anchors", resolved, len(anchors))
    return durations


async def _home_rain(
    session: Session, home: str, start: datetime, end: datetime,
) -> dict[str, int] | None:
    # One forecast at home stands in for every leg endpoint — city-scale
    # commutes don't cross weather systems.
    latlon = await geocode(session, home)
    if latlon is None:
        return None
    return await precipitation_probabilities_between(latlon[0], latlon[1], start, end) or None


# How far back the past-leg sweep looks. Wide enough to also catch strays
# that pre-window-clamp replans wrote next to years-old events.
_PAST_LEG_LOOKBACK = timedelta(days=1825)


async def delete_past_commute_legs(
    session: Session,
    *,
    account_key: str | None = None,
) -> int:
    """Delete managed commute events that already ended — a ride in the past
    is dead weight on the calendar."""
    write_calendar_id = (get_settings().google_calendar_id or "").strip()
    if not write_calendar_id:
        return 0
    now = datetime.now(timezone.utc)
    events = await list_events_between(
        session,
        calendar_ids=[write_calendar_id],
        time_min=now - _PAST_LEG_LOOKBACK,
        time_max=now,
        account_key=account_key,
    )
    deleted = 0
    for ev in events:
        if ev.all_day or not is_commute_event(ev) or ev.end > now:
            continue
        try:
            await delete_event(
                session, calendar_id=write_calendar_id, event_id=ev.id, account_key=account_key,
            )
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · past leg delete failed event=%s err=%s", ev.id, exc)
    return deleted


async def _resolve_tum_anchors(anchors: list[Anchor]) -> list[Anchor]:
    out: list[Anchor] = []
    for anchor in anchors:
        address = await resolve_tum_room(anchor.location)
        out.append(replace(anchor, location=address) if address else anchor)
    return out


async def _notify_new_failures(legs: list[PlannedLeg]) -> None:
    from app.services.notify import notify_unroutable_leg

    for leg in legs:
        await notify_unroutable_leg(
            origin=leg.origin,
            destination=leg.destination,
            depart=leg.depart,
            tag=f"route-{leg.origin_anchor}-{leg.dest_anchor}",
        )


async def _write_legs(
    session: Session,
    legs: list[PlannedLeg],
    *,
    calendar_id: str,
    existing: list[CalendarEvent],
    window: tuple[datetime, datetime],
    account_key: str | None,
) -> tuple[int, list[PlannedLeg]]:
    desired = {leg.key: leg for leg in legs}
    existing_by_key: dict[tuple[str, str], CalendarEvent] = {}
    legacy: list[CalendarEvent] = []
    for ev in existing:
        key = commute_leg_key(ev)
        if key is None:
            legacy.append(ev)
        else:
            existing_by_key[key] = ev

    for ev in legacy:
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        try:
            await delete_event(session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · legacy delete failed event=%s err=%s", ev.id, exc)

    for key, ev in existing_by_key.items():
        if key in desired:
            continue
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        try:
            await delete_event(session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · stale delete failed event=%s err=%s", ev.id, exc)

    written = 0
    new_failures: list[PlannedLeg] = []
    for key, leg in desired.items():
        summary = _summary_for(leg)
        description = _description_for(leg)
        props = commute_private_properties(origin_anchor=leg.origin_anchor, dest_anchor=leg.dest_anchor)
        reminders = _reminders_for_leg(leg)
        current = existing_by_key.get(key)
        # Notify only when a leg *becomes* failed — a replan that re-writes an
        # already-failed placeholder shouldn't re-alert on every pass.
        newly_failed = leg.mode == FAILED_MODE and (
            current is None or _commute_mode(current) != FAILED_MODE
        )
        try:
            if current is None:
                await create_event(
                    session,
                    calendar_id=calendar_id,
                    summary=summary,
                    start=leg.depart,
                    end=leg.arrive,
                    description=description,
                    location=leg.destination,
                    private_properties=props,
                    reminders=reminders,
                    account_key=account_key,
                )
            elif _needs_patch(current, leg, summary, description, reminders):
                await patch_event(
                    session,
                    calendar_id=calendar_id,
                    event_id=current.id,
                    summary=summary,
                    start=leg.depart,
                    end=leg.arrive,
                    description=description,
                    location=leg.destination,
                    private_properties=props,
                    reminders=reminders,
                    account_key=account_key,
                )
            written += 1
            if newly_failed:
                new_failures.append(leg)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · upsert failed origin=%s dest=%s err=%s",
                leg.origin_anchor, leg.dest_anchor, exc,
            )
    return written, new_failures


def _partition(
    events: list[CalendarEvent],
    write_calendar_id: str,
) -> tuple[list[Anchor], list[CalendarEvent], int]:
    anchors: list[Anchor] = []
    commutes: list[CalendarEvent] = []
    skipped_online = 0
    for ev in events:
        if ev.all_day:
            continue
        if is_commute_event(ev):
            if ev.calendar_id == write_calendar_id:
                commutes.append(ev)
            continue
        if is_online_location(ev.location):
            skipped_online += 1
            continue
        anchors.append(
            Anchor(
                id=ev.id,
                start=to_user_tz(ev.start),
                end=to_user_tz(ev.end),
                location=resolve_location_alias(ev.location),
            )
        )
    anchors.sort(key=lambda a: a.start)
    return anchors, commutes, skipped_online


def _read_calendar_ids(settings, write_calendar_id: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for cid in [write_calendar_id, *settings.google_busy_calendar_ids]:
        clean = (cid or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _context_margin(settings) -> timedelta:
    # Wide enough that an edge anchor's real neighbour — the one that decides
    # direct-leg vs via-home — is inside the fetched range even for long legs.
    return timedelta(
        minutes=max(
            120,
            2 * settings.commute_bike_max_minutes
            + settings.commute_home_layover_minutes
            + 2 * settings.commute_event_buffer_minutes,
        )
    )


def _needs_patch(
    ev: CalendarEvent,
    leg: PlannedLeg,
    summary: str,
    description: str,
    reminders: dict,
) -> bool:
    return (
        ev.summary != summary
        or (ev.description or "") != description
        or (ev.location or "") != leg.destination
        or _epoch(ev.start) != _epoch(leg.depart)
        or _epoch(ev.end) != _epoch(leg.arrive)
        or reminders_differ(ev, reminders)
    )


def _epoch(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _summary_for(leg: PlannedLeg) -> str:
    target = "home" if leg.dest_anchor == HOME else leg.destination.split(",")[0]
    if leg.mode == FAILED_MODE:
        return f"⚠️ Commute to {target} (no route)"
    mode = "Bike" if leg.mode == "bicycling" else "Transit"
    return f"{mode} {target}" if leg.dest_anchor == HOME else f"{mode} to {target}"


def _description_for(leg: PlannedLeg) -> str:
    lines = [f"From: {leg.origin}", f"To: {leg.destination}", f"Mode: {leg.mode}"]
    if leg.reason:
        lines.append(f"Reason: {leg.reason}")
    if leg.mode == FAILED_MODE:
        lines.append("No route found — reserved 30 min. Check the address.")
    lines.append(f"Navigate: {_navigation_url(leg)}")
    return "\n".join(lines)


def _navigation_url(leg: PlannedLeg) -> str:
    """Google Maps universal deep link — opens the Maps app with the route
    pre-filled (the Distance Matrix API itself returns no links). Failed
    legs get no travelmode so Maps picks whatever it can find."""
    params = {"api": "1", "origin": leg.origin, "destination": leg.destination}
    if leg.mode != FAILED_MODE:
        params["travelmode"] = leg.mode
    if leg.mode == "transit":
        params["departure_time"] = str(int(leg.depart.astimezone(timezone.utc).timestamp()))
    return f"https://www.google.com/maps/dir/?{urlencode(params)}"


def _commute_mode(ev: CalendarEvent) -> str | None:
    for line in (ev.description or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "mode":
            return value.strip().lower() or None
    return None


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


def _empty_summary() -> dict:
    return {
        "planned": 0,
        "skipped_online": 0,
        "skipped_unroutable": 0,
        "rescheduled_tasks": 0,
        "errors": [],
    }
