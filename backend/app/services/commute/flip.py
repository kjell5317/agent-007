"""Flip a commute leg's mode from the link in its calendar event.

A flip on an upcoming leg pins its mode (a strong preference, see
`app.services.plan.solver`) and re-plans; a flip on a leg that already
started records what actually happened, which moves the derived bike
location. Either way the flip is logged with the features of both modes —
the training row for the commute preferences."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

MODE_LABELS = {"bicycling": "Bike", "transit": "Transit", "failed": "No route"}
MODE_ICONS = {"bicycling": "🚲", "transit": "🚆", "failed": "⚠️"}
# A link survives re-plans that shift its leg by up to this much.
SEARCH_WINDOW = timedelta(days=1)


@dataclass(frozen=True)
class FlipRequest:
    origin_anchor: str
    dest_anchor: str
    depart: datetime
    mode: str


@dataclass(frozen=True)
class FlipPreview:
    current: str
    target: str
    place: str
    destination: str | None
    when: str


async def _find_leg(session: Session, request: FlipRequest):
    from app.services.calendar import commute_leg_key, list_events_between

    calendar_id = (get_settings().google_calendar_id or "").strip()
    if not calendar_id:
        raise LookupError("no write calendar")
    events = await list_events_between(
        session,
        calendar_ids=[calendar_id],
        time_min=request.depart - SEARCH_WINDOW,
        time_max=request.depart + SEARCH_WINDOW,
    )
    key = (request.origin_anchor, request.dest_anchor)
    matches = [ev for ev in events if commute_leg_key(ev) == key]
    if not matches:
        raise LookupError("leg not found")
    return min(matches, key=lambda ev: abs((ev.start - request.depart).total_seconds()))


def _place(address: str | None) -> str:
    return (address or "home").split(",")[0].strip() or "home"


async def describe_flip(session: Session, request: FlipRequest) -> FlipPreview:
    from app.services.commute.state import leg_record

    event = await _find_leg(session, request)
    record = leg_record(event)
    return FlipPreview(
        current=record.mode,
        target=request.mode,
        place=_place(record.destination),
        destination=record.destination,
        when=to_user_tz(record.depart).strftime("%a %H:%M"),
    )


async def apply_flip(session: Session, request: FlipRequest) -> str:
    from app.db.clients import commute_log as commute_log_store
    from app.services.commute.state import leg_record
    from app.services.learn.jobs import refit_soon

    event = await _find_leg(session, request)
    record = leg_record(event)
    if record.mode == request.mode:
        return f"This leg is already {MODE_LABELS[request.mode].lower()}."
    now = datetime.now(timezone.utc)
    past = record.depart <= now
    features = await _features(session, record, request.mode)
    commute_log_store.add(
        session,
        origin_anchor=record.origin_anchor,
        dest_anchor=record.dest_anchor,
        origin=record.origin,
        destination=record.destination,
        depart=record.depart,
        arrive=record.arrive,
        planned_mode=record.mode,
        flipped_mode=request.mode,
        past=past,
        features=features,
    )
    session.commit()
    refit_soon()
    if past:
        await _rewrite_past_leg(session, event, record, request.mode)
        await _replan(session, record)
        if request.mode == "bicycling":
            return f"Noted: you rode. The bike is at {_place(record.destination)}."
        return "Noted: you took transit."
    await _replan(session, record)
    try:
        current = leg_record(await _find_leg(session, request))
    except LookupError:
        return f"Switched to {MODE_LABELS[request.mode].lower()} — the trip was re-planned."
    if current.mode != request.mode:
        return (
            f"The bike isn't where this leg starts — it stays "
            f"{MODE_LABELS.get(current.mode, current.mode).lower()}."
        )
    return f"Switched to {MODE_LABELS[request.mode].lower()}."


async def _features(session: Session, record, flipped_mode: str) -> dict:
    """Features of the leg under both modes at its departure hour."""
    from app.services.commute.resolver import resolve_duration
    from app.services.commute.weather import geocode, hourly_forecast
    from app.services.plan import features as F

    settings = get_settings()
    out: dict = {}
    weather = None
    try:
        latlon = await geocode(session, settings.home_address)
        if latlon is not None:
            hour = to_user_tz(record.depart).replace(minute=0, second=0, microsecond=0)
            row = (await hourly_forecast(latlon[0], latlon[1], hour, hour + timedelta(hours=1))).get(
                hour.strftime("%Y-%m-%dT%H:00")
            )
            if row:
                weather = F.LegWeather(
                    rain_pct=row.get("rain_pct", 0.0), rain_mm=row.get("rain_mm", 0.0),
                    temp_c=row.get("temp_c"), wind_kmh=row.get("wind_kmh"),
                    dark=row.get("is_day", 1.0) == 0,
                )
    except Exception as exc:  # noqa: BLE001 — a flip is still worth logging without weather
        log.info("flip · weather unavailable err=%s", exc)
    for key, mode in (("planned", record.mode), ("flipped", flipped_mode)):
        minutes = None
        if record.origin and record.destination and mode in ("bicycling", "transit"):
            try:
                seconds = await resolve_duration(
                    session, origin=record.origin, destination=record.destination,
                    mode=mode, departure=record.depart,
                )
                minutes = seconds / 60 if seconds is not None else None
            except Exception as exc:  # noqa: BLE001
                log.info("flip · duration lookup failed mode=%s err=%s", mode, exc)
        if minutes is None:
            minutes = (record.arrive - record.depart).total_seconds() / 60
        out[key] = F.leg_features(
            mode, minutes, weather=weather,
            bike_max_minutes=settings.commute_bike_max_minutes,
            rain_threshold_pct=settings.commute_rain_threshold_pct,
        )
    return out


async def _rewrite_past_leg(session: Session, event, record, mode: str) -> None:
    """Make the calendar say what happened: the leg's mode is what locates
    the bike from now on."""
    from app.services.calendar import commute_private_properties, patch_event
    from app.services.commute.legs import PlannedLeg
    from app.services.commute.planner import _description_for, _summary_for

    leg = PlannedLeg(
        origin_anchor=record.origin_anchor,
        dest_anchor=record.dest_anchor,
        origin=record.origin or "",
        destination=record.destination or "",
        mode=mode,
        depart=record.depart,
        arrive=record.arrive,
    )
    props = commute_private_properties(
        origin_anchor=record.origin_anchor, dest_anchor=record.dest_anchor, mode=mode,
    )
    await patch_event(
        session,
        calendar_id=event.calendar_id,
        event_id=event.id,
        summary=_summary_for(leg),
        description=_description_for(leg),
        private_properties=props,
    )


async def _replan(session: Session, record) -> None:
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    margin = commute_window_margin()
    await plan_commutes_window_best_effort(
        session,
        window_start=max(record.depart, datetime.now(timezone.utc)) - margin,
        window_end=record.arrive + margin,
    )
