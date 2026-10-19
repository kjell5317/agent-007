"""Where the user and the bike are now, derived from the commute legs on
the calendar — the legs carry their mode, and a flipped past leg is
rewritten, so the calendar is the record of what happened."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.services.commute.legs import HOME
from app.timezones import to_user_tz

BIKE_LOOKBACK = timedelta(days=14)


@dataclass(frozen=True)
class LegRecord:
    origin_anchor: str
    dest_anchor: str
    origin: str | None
    destination: str | None
    mode: str
    depart: datetime
    arrive: datetime


@dataclass(frozen=True)
class CommuteState:
    # None = home.
    user_location: str | None = None
    user_anchor: str = HOME
    available_from: datetime | None = None
    bike_location: str | None = None
    bike_since: datetime | None = None
    # Legs not yet started, by (origin_anchor, dest_anchor): mode, departure.
    upcoming: dict[tuple[str, str], tuple[str, datetime]] = field(default_factory=dict)


def derive_state(legs: list[LegRecord], now: datetime) -> CommuteState:
    started = sorted((leg for leg in legs if leg.depart <= now), key=lambda leg: leg.depart)
    today = to_user_tz(now).date()
    user_location, user_anchor, available = None, HOME, None
    todays = [leg for leg in started if to_user_tz(leg.depart).date() == today]
    if todays:
        last = todays[-1]
        if last.dest_anchor != HOME:
            user_location, user_anchor = last.destination, last.dest_anchor
        if last.arrive > now:
            available = last.arrive
    bike_location, bike_since = None, None
    rides = [leg for leg in started if leg.mode == "bicycling" and leg.depart >= now - BIKE_LOOKBACK]
    if rides:
        last_ride = rides[-1]
        if last_ride.dest_anchor != HOME:
            bike_location, bike_since = last_ride.destination, last_ride.arrive
    upcoming = {
        (leg.origin_anchor, leg.dest_anchor): (leg.mode, leg.depart)
        for leg in legs
        if leg.depart > now
    }
    return CommuteState(
        user_location=user_location,
        user_anchor=user_anchor,
        available_from=available,
        bike_location=bike_location,
        bike_since=bike_since,
        upcoming=upcoming,
    )


def leg_record(event) -> LegRecord | None:
    from app.services.calendar import commute_leg_key
    from app.services.commute.planner import _commute_mode

    key = commute_leg_key(event)
    if key is None or event.all_day:
        return None
    origin = None
    for line in (event.description or "").splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip() == "From":
            origin = value.strip() or None
            break
    return LegRecord(
        origin_anchor=key[0],
        dest_anchor=key[1],
        origin=origin,
        destination=event.location,
        mode=_commute_mode(event) or "transit",
        depart=to_user_tz(event.start),
        arrive=to_user_tz(event.end),
    )


async def load_legs(
    session: Session, start: datetime, end: datetime, *, account_key: str | None = None
) -> tuple[list[LegRecord], list]:
    """Commute legs on the write calendar in `[start, end)`, plus the raw
    events (the leg writer diffs against them)."""
    from app.config import get_settings
    from app.services.calendar import is_commute_event, list_events_between

    calendar_id = (get_settings().google_calendar_id or "").strip()
    if not calendar_id:
        return [], []
    events = await list_events_between(
        session, calendar_ids=[calendar_id], time_min=start, time_max=end, account_key=account_key,
    )
    raw = [ev for ev in events if is_commute_event(ev) and not ev.all_day]
    records = [record for ev in raw if (record := leg_record(ev)) is not None]
    return records, events


async def load_state(session: Session, now: datetime, horizon_end: datetime) -> tuple[CommuteState, list]:
    records, events = await load_legs(session, now - BIKE_LOOKBACK, horizon_end)
    return derive_state(records, now), events
