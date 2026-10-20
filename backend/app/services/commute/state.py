"""Where the user and the bike are now.

Finished legs are folded into a stored `Position` (the hourly cleanup does
it right before deleting them); legs that have started since are replayed
on top from the calendar. A leg carries its mode, and a leg flipped while
underway is rewritten, so what gets folded is what happened."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.services.commute.legs import HOME
from app.timezones import to_user_tz

# Started legs still on the calendar: the cleanup removes them within the hour.
REPLAY_LOOKBACK = timedelta(days=1)


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
class Position:
    # None = home.
    bike_location: str | None = None
    bike_anchor: str = HOME
    bike_since: datetime | None = None
    user_location: str | None = None
    user_anchor: str = HOME
    user_since: datetime | None = None
    applied_through: datetime | None = None


def advance(position: Position, legs) -> Position:
    """Fold legs into the position, in departure order, each at most once."""
    out = position
    for leg in sorted(legs, key=lambda leg: leg.depart):
        if out.applied_through is not None and leg.depart <= out.applied_through:
            continue
        home = leg.dest_anchor == HOME
        where = None if home else leg.destination
        out = Position(
            bike_location=where if leg.mode == "bicycling" else out.bike_location,
            bike_anchor=leg.dest_anchor if leg.mode == "bicycling" else out.bike_anchor,
            bike_since=leg.arrive if leg.mode == "bicycling" else out.bike_since,
            user_location=where,
            user_anchor=leg.dest_anchor,
            user_since=leg.arrive,
            applied_through=leg.depart,
        )
    return out


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


def derive_state(legs: list[LegRecord], now: datetime, base: Position | None = None) -> CommuteState:
    position = advance(base or Position(), [leg for leg in legs if leg.depart <= now])
    user_location, user_anchor, available = None, HOME, None
    arrived_today = (
        position.user_since is not None
        and to_user_tz(position.user_since).date() == to_user_tz(now).date()
    )
    if arrived_today:
        user_location, user_anchor = position.user_location, position.user_anchor
        if position.user_since > now:
            available = position.user_since
    upcoming = {
        (leg.origin_anchor, leg.dest_anchor): (leg.mode, leg.depart)
        for leg in legs
        if leg.depart > now
    }
    return CommuteState(
        user_location=user_location,
        user_anchor=user_anchor if user_location else HOME,
        available_from=available,
        bike_location=position.bike_location,
        bike_since=position.bike_since,
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
    from app.db.clients import commute_state as commute_state_store

    records, events = await load_legs(session, now - REPLAY_LOOKBACK, horizon_end)
    return derive_state(records, now, commute_state_store.get(session)), events


def fold_finished(session: Session, legs: list[LegRecord]) -> None:
    """Persist finished legs into the stored position (before they're deleted)."""
    from app.db.clients import commute_state as commute_state_store

    position = commute_state_store.get(session)
    advanced = advance(position, legs)
    if advanced != position:
        commute_state_store.save(session, advanced)


def forget(position: Position, anchors: set[str]) -> Position:
    """Undo a folded arrival at a deleted event: that trip didn't happen."""
    out = position
    if out.user_anchor in anchors:
        out = replace(out, user_location=None, user_anchor=HOME, user_since=None)
    if out.bike_anchor in anchors:
        out = replace(out, bike_location=None, bike_anchor=HOME, bike_since=None)
    return out


async def forget_trips_to(session: Session, anchors: set[str], now: datetime) -> None:
    """Trips to deleted events didn't happen. The replan drops their future
    legs but leaves started ones as history — replayed, those would keep the
    user at the event all day and plan a ride home from it."""
    from app.config import get_settings
    from app.db.clients import commute_state as commute_state_store
    from app.services.calendar import commute_leg_key, delete_event

    settings = get_settings()
    if not settings.commute_enabled:
        return
    _, events = await load_legs(session, now - REPLAY_LOOKBACK, now)
    for ev in events:
        key = commute_leg_key(ev)
        if key is not None and anchors & set(key):
            await delete_event(session, calendar_id=ev.calendar_id, event_id=ev.id)
    position = commute_state_store.get(session)
    forgotten = forget(position, anchors)
    if forgotten != position:
        commute_state_store.save(session, forgotten)
