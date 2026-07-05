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
    resolve_routable_location,
)
from app.timezones import to_user_tz

log = logging.getLogger(__name__)


async def refresh_weather_sensitive_commutes(
    session: Session,
    *,
    account_key: str | None = None,
    _depth: int = 0,
) -> dict:
    """Re-derive commute legs for the next day when the forecast would flip
    modes: rain flips bike trips to transit, and a cleared forecast flips
    rain-forced transit trips back to bike (re-derivation decides from
    scratch, so threshold- or route-forced transit stays transit)."""
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
    rainy = rain_pct is not None and rain_pct >= settings.commute_rain_threshold_pct

    events = await list_events_between(
        session,
        calendar_ids=[write_calendar_id],
        time_min=now,
        time_max=window_end,
        account_key=account_key,
    )
    # Rain can only flip bike legs; a dry forecast can only flip transit
    # legs back. No candidate legs → nothing the weather could change.
    flippable = "bicycling" if rainy else "transit"
    if not any(is_commute_event(ev) and _commute_mode(ev) == flippable for ev in events):
        log.info(
            "commute weather refresh skipped · rain=%s threshold=%s flippable=%s",
            rain_pct,
            settings.commute_rain_threshold_pct,
            flippable,
        )
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

    # Legs only exist inside [now, now + lookahead]: past legs can't be
    # ridden, and legs further out than the horizon are never written — a
    # task placed weeks ahead (due date minus lead) gets its legs once the
    # daily re-baseline slides it into the window. This also keeps stale
    # caller windows (e.g. re-placing a long-overdue task whose prior slot
    # is months back) from fanning out over historical events.
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=settings.commute_lookahead_days)
    window_start = max(window_start, now)
    if window_end > horizon:
        log.info(
            "commute · window capped at lookahead horizon %s (requested until %s)",
            horizon.isoformat(),
            window_end.isoformat(),
        )
        window_end = horizon
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
    anchors, existing_commutes, online = _partition(events, write_calendar_id)
    anchors = await _resolve_routable_anchors(anchors)
    summary["skipped_online"] = len(online)

    # An anchor straddling the window edge gets ALL its legs written (see
    # `_legs_to_write`) — but if the anchor extends past the fetched range,
    # its existing legs out there are invisible to the diff, which would
    # duplicate instead of patch them. Refetch with the full extent.
    touching = [a for a in anchors if _overlaps(a.start, a.end, window_start, window_end)]
    if touching:
        need_lo = min(min(a.start for a in touching) - margin, window_start - margin)
        need_hi = max(max(a.end for a in touching) + margin, window_end + margin)
        if need_lo < window_start - margin or need_hi > window_end + margin:
            log.info(
                "commute · widening fetch to %s..%s for edge anchors",
                need_lo.isoformat(),
                need_hi.isoformat(),
            )
            events = await list_events_between(
                session,
                calendar_ids=_read_calendar_ids(settings, write_calendar_id),
                time_min=need_lo,
                time_max=need_hi,
                account_key=account_key,
            )
            anchors, existing_commutes, online = _partition(events, write_calendar_id)
            anchors = await _resolve_routable_anchors(anchors)
            summary["skipped_online"] = len(online)

    online_spans = [(to_user_tz(ev.start), to_user_tz(ev.end)) for ev in online]

    if hourly_rain is None:
        hourly_rain = await _home_rain(session, home, window_start, window_end + margin)

    durations = await _resolve_routes(
        session,
        anchors,
        home,
        hourly_rain,
        settings,
        summary,
    )
    if durations is None:
        # Transient Maps failure — anything derived now would turn every leg
        # into a bogus "no route" placeholder. Leave the calendar untouched;
        # the next scheduled pass retries.
        return summary
    # The chain rules (bike stays home / one mode per trip) can force transit
    # onto pairs the optimistic first pass didn't fetch — and each re-derive
    # may surface further pairs. Fetch just those and loop; two extra rounds
    # cover any chain in practice.
    legs: list[PlannedLeg] = []
    skipped_unroutable = 0
    for _ in range(3):
        missing_transit: set[tuple[str, str]] = set()
        legs, skipped_unroutable = derive_legs(
            anchors,
            home,
            durations,
            hourly_rain,
            settings,
            avoid=online_spans,
            missing_transit=missing_transit,
        )
        if not missing_transit:
            break
        if not await _resolve_missing_transit(
            session,
            missing_transit,
            anchors,
            home,
            durations,
            summary,
        ):
            return summary
    summary["skipped_unroutable"] = skipped_unroutable

    to_write = _legs_to_write(legs, anchors, window_start, window_end)

    # A task anchor whose legs collide with another anchor has a slot with
    # no room for its trip (typically placed before trip-block planning
    # existed). Re-place the task instead of writing overlapping legs — its
    # own replan then derives clean ones.
    task_event_ids = {ev.id for ev in events if is_task_event(ev)}
    needs_replacement = _reschedule_candidates(to_write, anchors, task_event_ids)
    if needs_replacement:
        to_write = [leg for leg in to_write if not (set(leg.key) & needs_replacement)]

    # Whatever still collides after task re-placement involves only fixed
    # events — nothing can move, so the leg is written anyway, the conflict
    # stays visible on the calendar, and the user gets one alert (tracked
    # via a marker on the leg so replans don't re-alert).
    summaries = {ev.id: ev.summary for ev in events}
    obstacles = [(a.id, summaries.get(a.id) or a.location, a.start, a.end) for a in anchors] + [
        (ev.id, ev.summary, to_user_tz(ev.start), to_user_tz(ev.end)) for ev in online
    ]
    conflicts = _immovable_conflicts(to_write, obstacles)
    for key, (blocker_id, blocker_label) in conflicts.items():
        log.warning(
            "commute · leg %s -> %s overlaps immovable %r (id=%s); leaving the conflict visible",
            key[0],
            key[1],
            blocker_label,
            blocker_id,
        )

    summary["planned"], new_failures, new_conflicts = await _write_legs(
        session,
        to_write,
        calendar_id=write_calendar_id,
        existing=existing_commutes,
        window=(window_start, window_end),
        account_key=account_key,
        conflicts=conflicts,
    )
    await _notify_new_failures(new_failures)
    await _notify_new_conflicts(new_conflicts)

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
        desired = (
            SILENT_REMINDERS
            if has_arriving_leg
            else popup_reminders(settings.reminder_lead_minutes)
        )
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


def _legs_to_write(
    legs: list[PlannedLeg],
    anchors: list[Anchor],
    window_start: datetime,
    window_end: datetime,
) -> list[PlannedLeg]:
    """Legs the replan may write or delete.

    Legs of any anchor inside the window are included even though they sit
    just *outside* it (an arrival ends `buffer` before its anchor starts) —
    filtering by leg-span alone dropped every leg of a bare created/edited
    event span. Legs merely poking into the window from edge anchors are
    still written; the fetch margin only exists so those anchors see their
    real neighbours."""
    in_window = {
        anchor.id
        for anchor in anchors
        if _overlaps(anchor.start, anchor.end, window_start, window_end)
    }
    return [
        leg
        for leg in legs
        if _overlaps(leg.depart, leg.arrive, window_start, window_end) or in_window & set(leg.key)
    ]


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
    durations: Durations = {}
    resolved = 0
    for (origin, destination), reference in required_routes(anchors, home).items():
        try:
            bike = await resolve_duration(
                session,
                origin=origin,
                destination=destination,
                mode="bicycling",
                departure=reference,
            )
            durations[(origin, destination, "bicycling")] = bike
            resolved += 1
            if bike is None or bike > settings.commute_bike_max_minutes * 60 or rain_possible:
                durations[(origin, destination, "transit")] = await resolve_duration(
                    session,
                    origin=origin,
                    destination=destination,
                    mode="transit",
                    departure=reference,
                )
                resolved += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · route resolution aborted at %s -> %s err=%s",
                origin,
                destination,
                exc,
            )
            summary["errors"].append(
                {
                    "route": f"{origin} -> {destination}",
                    "error": str(exc),
                    "transient": True,
                }
            )
            return None
    if resolved:
        log.info("commute · resolved %d route durations for %d anchors", resolved, len(anchors))
    return durations


async def _resolve_missing_transit(
    session: Session,
    pairs: set[tuple[str, str]],
    anchors: list[Anchor],
    home: str,
    durations: Durations,
    summary: dict,
) -> bool:
    """Second-pass transit lookups for pairs the chain rule forced onto
    transit after the optimistic first resolution — fetched lazily so the
    common all-bike day costs no extra Distance Matrix elements."""
    references = required_routes(anchors, home)
    for origin, destination in pairs:
        try:
            durations[(origin, destination, "transit")] = await resolve_duration(
                session,
                origin=origin,
                destination=destination,
                mode="transit",
                departure=references.get((origin, destination)),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · transit resolution aborted at %s -> %s err=%s",
                origin,
                destination,
                exc,
            )
            summary["errors"].append(
                {
                    "route": f"{origin} -> {destination}",
                    "error": str(exc),
                    "transient": True,
                }
            )
            return False
    return True


async def _home_rain(
    session: Session,
    home: str,
    start: datetime,
    end: datetime,
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
# How far beyond the lookahead horizon the stray sweep looks — far-future
# legs come from task placements at `due - lead`, so a year covers any
# realistic due date.
_STRAY_LEG_LOOKAHEAD = timedelta(days=365)


async def delete_stray_commute_legs(
    session: Session,
    *,
    account_key: str | None = None,
) -> int:
    """Delete managed commute events outside `[now, now + lookahead]` — a
    ride in the past is dead weight, and legs written beyond the horizon
    (strays from before the horizon cap) get re-derived once their anchor
    slides into the window."""
    settings = get_settings()
    write_calendar_id = (settings.google_calendar_id or "").strip()
    if not write_calendar_id:
        return 0
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=settings.commute_lookahead_days)
    deleted = 0
    for time_min, time_max in (
        (now - _PAST_LEG_LOOKBACK, now),
        (horizon, horizon + _STRAY_LEG_LOOKAHEAD),
    ):
        events = await list_events_between(
            session,
            calendar_ids=[write_calendar_id],
            time_min=time_min,
            time_max=time_max,
            account_key=account_key,
        )
        for ev in events:
            if ev.all_day or not is_commute_event(ev):
                continue
            if ev.end > now and ev.start < horizon:
                # Running or boundary-straddling leg — still live.
                continue
            try:
                await delete_event(
                    session,
                    calendar_id=write_calendar_id,
                    event_id=ev.id,
                    account_key=account_key,
                )
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("commute · stray leg delete failed event=%s err=%s", ev.id, exc)
    return deleted


async def _resolve_routable_anchors(anchors: list[Anchor]) -> list[Anchor]:
    out: list[Anchor] = []
    for anchor in anchors:
        location = await resolve_routable_location(anchor.location)
        out.append(replace(anchor, location=location) if location else anchor)
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


def _immovable_conflicts(
    legs: list[PlannedLeg],
    obstacles: list[tuple[str, str, datetime, datetime]],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Legs still colliding with an `(id, label, start, end)` obstacle after
    task re-placement — located anchors and online/location-less events the
    dodge couldn't clear. Only immovable things remain in their way."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for leg in legs:
        for oid, label, start, end in obstacles:
            if oid in leg.key:
                continue
            if leg.depart < end and start < leg.arrive:
                out[leg.key] = (oid, label)
                break
    return out


def _conflict_tag(key: tuple[str, str]) -> str:
    return f"conflict-{key[0]}-{key[1]}"


async def _notify_new_conflicts(conflicts: list[tuple[PlannedLeg, str]]) -> None:
    from app.services.notify import notify_leg_conflict

    for leg, blocker_label in conflicts:
        await notify_leg_conflict(
            destination=leg.destination,
            blocker=blocker_label,
            depart=leg.depart,
            tag=_conflict_tag(leg.key),
        )


async def _write_legs(
    session: Session,
    legs: list[PlannedLeg],
    *,
    calendar_id: str,
    existing: list[CalendarEvent],
    window: tuple[datetime, datetime],
    account_key: str | None,
    conflicts: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[int, list[PlannedLeg], list[tuple[PlannedLeg, str]]]:
    conflicts = conflicts or {}
    desired = {leg.key: leg for leg in legs}
    existing_by_key: dict[tuple[str, str], CalendarEvent] = {}
    legacy: list[CalendarEvent] = []
    duplicates: list[CalendarEvent] = []
    for ev in existing:
        key = commute_leg_key(ev)
        if key is None:
            legacy.append(ev)
        elif key in existing_by_key:
            # Same leg identity written twice (a diff that couldn't see the
            # first copy) — redundant by construction, delete on sight.
            duplicates.append(ev)
        else:
            existing_by_key[key] = ev

    for ev in duplicates:
        log.warning(
            "commute · duplicate leg %s (%s) — deleting",
            commute_leg_key(ev),
            ev.id,
        )
        try:
            await delete_event(
                session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · duplicate delete failed event=%s err=%s", ev.id, exc)

    for ev in legacy:
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        try:
            await delete_event(
                session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · legacy delete failed event=%s err=%s", ev.id, exc)

    for key, ev in existing_by_key.items():
        if key in desired:
            continue
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        try:
            await delete_event(
                session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · stale delete failed event=%s err=%s", ev.id, exc)

    written = 0
    new_failures: list[PlannedLeg] = []
    new_conflicts: list[tuple[PlannedLeg, str]] = []
    for key, leg in desired.items():
        summary = _summary_for(leg)
        description = _description_for(leg)
        blocker = conflicts.get(key)
        props = commute_private_properties(
            origin_anchor=leg.origin_anchor,
            dest_anchor=leg.dest_anchor,
            mode=leg.mode,
        )
        # Conflict marker: persisted on the leg so replans that re-derive the
        # same collision don't re-alert; cleared (empty) when it resolves.
        props["conflict"] = blocker[0] if blocker is not None else ""
        reminders = _reminders_for_leg(leg)
        current = existing_by_key.get(key)
        was_conflicting = bool(private_properties(current).get("conflict")) if current else False
        # Notify only when a leg *becomes* failed/conflicting — a replan that
        # re-writes the same state shouldn't re-alert on every pass.
        newly_failed = leg.mode == FAILED_MODE and (
            current is None or _commute_mode(current) != FAILED_MODE
        )
        newly_conflicting = blocker is not None and not was_conflicting
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
            elif _needs_patch(current, leg, summary, description, reminders, props["conflict"]):
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
            if newly_conflicting:
                new_conflicts.append((leg, blocker[1]))
            elif blocker is None and was_conflicting:
                from app.services.notify import clear_notification_tag

                await clear_notification_tag(_conflict_tag(key))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · upsert failed origin=%s dest=%s err=%s",
                leg.origin_anchor,
                leg.dest_anchor,
                exc,
            )
    return written, new_failures, new_conflicts


def _partition(
    events: list[CalendarEvent],
    write_calendar_id: str,
) -> tuple[list[Anchor], list[CalendarEvent], list[CalendarEvent]]:
    """Split events into routable anchors, managed commute legs, and online
    or location-less events — not routed to, but legs shouldn't sit on top
    of them either."""
    anchors: list[Anchor] = []
    commutes: list[CalendarEvent] = []
    online: list[CalendarEvent] = []
    for ev in events:
        if ev.all_day:
            continue
        if is_commute_event(ev):
            if ev.calendar_id == write_calendar_id:
                commutes.append(ev)
            continue
        if is_online_location(ev.location):
            online.append(ev)
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
    return anchors, commutes, online


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
    conflict_marker: str = "",
) -> bool:
    # The Updated stamp changes on every derivation — comparing it would turn
    # every replan into a patch of every leg.
    return (
        ev.summary != summary
        or _without_updated_line(ev.description or "") != _without_updated_line(description)
        or (ev.location or "") != leg.destination
        or _epoch(ev.start) != _epoch(leg.depart)
        or _epoch(ev.end) != _epoch(leg.arrive)
        or reminders_differ(ev, reminders)
        or (private_properties(ev).get("conflict") or "") != conflict_marker
    )


def _without_updated_line(description: str) -> str:
    return "\n".join(line for line in description.splitlines() if not line.startswith("Updated:"))


def _epoch(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _summary_for(leg: PlannedLeg) -> str:
    # The destination already lives in the event's location field.
    if leg.mode == FAILED_MODE:
        return "⚠️ No route"
    return "🚲 Travel" if leg.mode == "bicycling" else "🚆 Travel"


def _description_for(leg: PlannedLeg) -> str:
    # Destination sits in the location field, the mode in the emoji title.
    lines = [f"From: {leg.origin}"]
    if leg.reason:
        lines.append(f"Reason: {leg.reason}")
    if leg.mode == FAILED_MODE:
        lines.append("No route found — reserved 30 min. Check the address.")
    lines.append(f"Navigate: {_navigation_url(leg)}")
    updated = to_user_tz(datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    lines.append(f"Updated: {updated}")
    return "\n".join(lines)


def _navigation_url(leg: PlannedLeg) -> str:
    """Google Maps universal deep link — opens the Maps app with the route
    pre-filled (the Distance Matrix API itself returns no links). Failed
    legs get no travelmode so Maps picks whatever it can find. No
    departure_time: the deep link ignores it."""
    params = {"api": "1", "origin": leg.origin, "destination": leg.destination}
    if leg.mode != FAILED_MODE:
        params["travelmode"] = leg.mode
    return f"https://www.google.com/maps/dir/?{urlencode(params)}"


def _commute_mode(ev: CalendarEvent) -> str | None:
    mode = ev.private_properties.get("mode")
    if mode:
        return mode
    # Legacy legs carried the mode as a description line.
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
