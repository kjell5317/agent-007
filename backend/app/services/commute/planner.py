"""Commute event planner.

This creates managed commute events for physical events and then lets the task
scheduler repair any task that those commute windows collide with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.calendar import (
    CalendarEvent,
    commute_private_properties,
    create_event,
    delete_event,
    is_commute_event,
    list_events_between,
    patch_event,
    private_properties,
)
from app.services.commute.resolver import resolve_duration
from app.services.commute.weather import (
    geocode,
    precipitation_probability_at,
    precipitation_probabilities_between,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommutePlan:
    leg: str
    origin: str
    destination: str
    mode: str
    depart: datetime
    arrive: datetime
    related_event_id: str
    mode_reason: str | None = None


async def refresh_weather_sensitive_commutes(
    session: Session,
    *,
    account_key: str | None = None,
) -> dict:
    """Refresh existing commute events only when the next day has rain."""
    settings = get_settings()
    summary = _empty_summary()
    write_calendar_id = (settings.google_calendar_id or "").strip()
    if not write_calendar_id:
        summary["errors"].append({"setup": "google_calendar_id not configured"})
        return summary
    if not settings.home_address:
        summary["errors"].append({"setup": "home_address not configured"})
        return summary

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=1)
    home_latlon = await geocode(settings.home_address)
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
    related_event_ids = {
        related
        for ev in events
        if is_commute_event(ev) and _commute_mode(ev) == "bicycling"
        if (related := private_properties(ev).get("related_event_id"))
    }
    if not related_event_ids:
        return _empty_summary()

    return await plan_window_commutes(
        session,
        window_start=now,
        window_end=window_end,
        target_event_ids=related_event_ids,
        stale_event_ids=related_event_ids,
        hourly_rain=hourly_rain,
        account_key=account_key,
    )


async def plan_window_commutes(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    target_event_ids: set[str] | None = None,
    stale_event_ids: set[str] | None = None,
    hourly_rain: dict[str, int] | None = None,
    account_key: str | None = None,
) -> dict:
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
        summary["errors"].append({"setup": "google_maps_api_key not configured"})
        return summary

    margin = _context_margin(settings)
    events = await list_events_between(
        session,
        calendar_ids=_read_calendar_ids(settings, write_calendar_id),
        time_min=window_start - margin,
        time_max=window_end + margin,
        account_key=account_key,
    )
    physical, existing_commutes = _partition(events, write_calendar_id)
    targets = _targets(physical, window_start, window_end, target_event_ids)
    summary["skipped_online"] = len(events) - len(physical) - len(existing_commutes)

    plans: list[CommutePlan] = []
    for ev in targets:
        try:
            plans.extend(await _plans_for_event(session, ev, settings, hourly_rain=hourly_rain))
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · event=%s planning failed err=%s", ev.id, exc)
            summary["errors"].append({"event_id": ev.id, "error": str(exc)})

    summary["planned"] = await _write_plans(
        session,
        plans,
        calendar_id=write_calendar_id,
        existing_commutes=existing_commutes,
        stale_window=(window_start, window_end),
        stale_event_ids=stale_event_ids if stale_event_ids is not None else target_event_ids,
        account_key=account_key,
    )

    from app.services.commute.reschedule import reschedule_overlapping_tasks

    summary["rescheduled_tasks"] = await reschedule_overlapping_tasks(
        session,
        plans,
        account_key=account_key,
    )
    return summary


async def _plans_for_event(
    session: Session,
    ev: CalendarEvent,
    settings,
    *,
    hourly_rain: dict[str, int] | None,
) -> list[CommutePlan]:
    if not ev.location:
        return []

    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    home = settings.home_address
    weather_latlon = None if hourly_rain is not None else await geocode(ev.location)
    out: list[CommutePlan] = []

    outbound = await _build_leg(
        session,
        origin=home,
        destination=ev.location,
        arrive_by=ev.start - buffer,
        depart_at=None,
        hourly_rain=hourly_rain,
        weather_latlon=weather_latlon,
        settings=settings,
        related_event_id=ev.id,
        leg="outbound",
    )
    if outbound is not None:
        out.append(outbound)

    inbound = await _build_leg(
        session,
        origin=ev.location,
        destination=home,
        arrive_by=None,
        depart_at=ev.end + buffer,
        hourly_rain=hourly_rain,
        weather_latlon=weather_latlon,
        settings=settings,
        related_event_id=ev.id,
        leg="inbound",
    )
    if inbound is not None:
        out.append(inbound)
    return out


async def _build_leg(
    session: Session,
    *,
    origin: str,
    destination: str,
    arrive_by: datetime | None,
    depart_at: datetime | None,
    hourly_rain: dict[str, int] | None,
    weather_latlon: tuple[float, float] | None,
    settings,
    related_event_id: str,
    leg: str,
) -> CommutePlan | None:
    if origin == destination:
        return None

    reference = depart_at or (arrive_by - timedelta(hours=1))  # type: ignore[operator]
    bike_seconds = await resolve_duration(
        session,
        origin=origin,
        destination=destination,
        mode="bicycling",
        departure=reference,
    )
    mode = "bicycling"
    duration_s = bike_seconds
    reason = None

    rain_pct = None
    when_for_weather = arrive_by or depart_at
    if hourly_rain is not None and when_for_weather is not None:
        rain_pct = _rain_at(hourly_rain, when_for_weather)
    elif weather_latlon is not None and when_for_weather is not None:
        rain_pct = await precipitation_probability_at(
            weather_latlon[0],
            weather_latlon[1],
            when_for_weather,
        )

    if bike_seconds is None or bike_seconds > settings.commute_bike_max_minutes * 60:
        mode = "transit"
        reason = "bike unavailable" if bike_seconds is None else "bike exceeds threshold"
    elif rain_pct is not None and rain_pct >= settings.commute_rain_threshold_pct:
        mode = "transit"
        reason = f"rain {rain_pct}% >= {settings.commute_rain_threshold_pct}% threshold"

    if mode == "transit":
        duration_s = await resolve_duration(
            session,
            origin=origin,
            destination=destination,
            mode="transit",
            departure=reference,
        )
    if duration_s is None:
        if bike_seconds is None:
            return None
        mode = "bicycling"
        duration_s = bike_seconds
        reason = "transit unavailable, fell back to bike"

    travel = timedelta(seconds=duration_s)
    if arrive_by is not None:
        depart = arrive_by - travel
        arrive = arrive_by
    else:
        assert depart_at is not None
        depart = depart_at
        arrive = depart_at + travel
    return CommutePlan(
        leg=leg,
        origin=origin,
        destination=destination,
        mode=mode,
        depart=depart,
        arrive=arrive,
        related_event_id=related_event_id,
        mode_reason=reason,
    )


async def _write_plans(
    session: Session,
    plans: list[CommutePlan],
    *,
    calendar_id: str,
    existing_commutes: list[CalendarEvent],
    stale_window: tuple[datetime, datetime],
    stale_event_ids: set[str] | None,
    account_key: str | None,
) -> int:
    desired = {_key_for_plan(plan): plan for plan in plans}
    existing = {
        key: ev
        for ev in existing_commutes
        if (key := _key_for_event(ev)) is not None
    }

    for key, ev in existing.items():
        if key in desired:
            continue
        if not _should_delete_stale(key, ev, stale_window, stale_event_ids):
            continue
        try:
            await delete_event(session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · stale delete failed event=%s err=%s", ev.id, exc)

    written = 0
    for key, plan in desired.items():
        summary = _summary_for(plan)
        description = _description_for(plan)
        props = commute_private_properties(related_event_id=plan.related_event_id, leg=plan.leg)
        current = existing.get(key)
        try:
            if current is None:
                await create_event(
                    session,
                    calendar_id=calendar_id,
                    summary=summary,
                    start=plan.depart,
                    end=plan.arrive,
                    description=description,
                    location=plan.destination,
                    private_properties=props,
                    account_key=account_key,
                )
            elif _needs_patch(current, plan, summary, description):
                await patch_event(
                    session,
                    calendar_id=calendar_id,
                    event_id=current.id,
                    summary=summary,
                    start=plan.depart,
                    end=plan.arrive,
                    description=description,
                    location=plan.destination,
                    private_properties=props,
                    account_key=account_key,
                )
            written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · upsert failed related=%s leg=%s err=%s", plan.related_event_id, plan.leg, exc)
    return written


def _partition(events: list[CalendarEvent], write_calendar_id: str) -> tuple[list[CalendarEvent], list[CalendarEvent]]:
    physical: list[CalendarEvent] = []
    commutes: list[CalendarEvent] = []
    for ev in events:
        if ev.all_day:
            continue
        if is_commute_event(ev) and ev.calendar_id == write_calendar_id:
            commutes.append(ev)
            continue
        if is_commute_event(ev):
            continue
        if _is_online(ev):
            continue
        physical.append(ev)
    physical.sort(key=lambda ev: ev.start)
    return physical, commutes


def _targets(
    physical: list[CalendarEvent],
    window_start: datetime,
    window_end: datetime,
    target_event_ids: set[str] | None,
) -> list[CalendarEvent]:
    if target_event_ids is not None:
        return [ev for ev in physical if ev.id in target_event_ids]
    return [ev for ev in physical if _overlaps(ev.start, ev.end, window_start, window_end)]


def _read_calendar_ids(settings, write_calendar_id: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for cid in [write_calendar_id, *settings.google_busy_calendar_ids]:
        clean = (cid or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _is_online(ev: CalendarEvent) -> bool:
    loc = (ev.location or "").strip()
    return not loc or loc.startswith(("http://", "https://")) or "zoom.us" in loc.lower()


def _context_margin(settings) -> timedelta:
    return timedelta(
        minutes=max(
            settings.commute_bike_max_minutes,
            settings.commute_home_layover_minutes * 2,
            settings.commute_event_buffer_minutes,
        )
    )


def _key_for_plan(plan: CommutePlan) -> tuple[str, str]:
    return plan.related_event_id, plan.leg


def _key_for_event(ev: CalendarEvent) -> tuple[str, str] | None:
    props = private_properties(ev)
    related = props.get("related_event_id")
    leg = props.get("leg")
    return (related, leg) if related and leg else None


def _should_delete_stale(
    key: tuple[str, str],
    ev: CalendarEvent,
    stale_window: tuple[datetime, datetime],
    stale_event_ids: set[str] | None,
) -> bool:
    if stale_event_ids is not None:
        return key[0] in stale_event_ids
    return _overlaps(ev.start, ev.end, stale_window[0], stale_window[1])


def _needs_patch(ev: CalendarEvent, plan: CommutePlan, summary: str, description: str) -> bool:
    return (
        ev.summary != summary
        or (ev.description or "") != description
        or (ev.location or "") != plan.destination
        or _epoch(ev.start) != _epoch(plan.depart)
        or _epoch(ev.end) != _epoch(plan.arrive)
    )


def _epoch(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _summary_for(plan: CommutePlan) -> str:
    mode = "Bike" if plan.mode == "bicycling" else "Transit"
    return f"{mode} to {plan.destination.split(',')[0]}"


def _description_for(plan: CommutePlan) -> str:
    lines = [f"From: {plan.origin}", f"To: {plan.destination}", f"Mode: {plan.mode}"]
    if plan.mode_reason:
        lines.append(f"Reason: {plan.mode_reason}")
    return "\n".join(lines)


def _commute_mode(ev: CalendarEvent) -> str | None:
    for line in (ev.description or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "mode":
            return value.strip().lower() or None
    return None


def _rain_at(hourly_rain: dict[str, int], when: datetime) -> int | None:
    from app.timezones import to_user_tz

    return hourly_rain.get(to_user_tz(when).strftime("%Y-%m-%dT%H:00"))


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
