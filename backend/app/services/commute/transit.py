"""Real transit connections for the written legs.

The solver models a transit leg with the slowest of three reference trip
durations, but buses run on a timetable: the modelled "leave 8:12" may have
no bus behind it. Before the legs are written, every transit leg in the
next 36 h is swapped for a real connection that fits between whatever comes
before and after it, and its line and stop go into the description.

Preference: the first connection from the modelled departure, else the last
one arriving in time; both keep the commute buffer on each side first and
only then give it up. A leg nothing fits, or a failed lookup, keeps the
modelled times.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta

import httpx

from app.services.commute import client as maps
from app.services.commute.client import MapsLookupError, TransitConnection
from app.services.commute.legs import PlannedLeg

log = logging.getLogger(__name__)

LOOKAHEAD = timedelta(hours=36)
CACHE_TTL_SECONDS = 3600.0
FAILURE_BACKOFF_SECONDS = 600.0

Window = tuple[datetime | None, datetime | None]

_cache: dict[tuple[str, str, str, int], tuple[float, TransitConnection | None]] = {}
_backoff_until = 0.0


async def fit_legs(
    legs: list[PlannedLeg],
    obstacles: list[tuple[datetime, datetime]],
    *,
    now: datetime,
    buffer: timedelta,
) -> list[PlannedLeg]:
    out = list(legs)
    for i in sorted(range(len(out)), key=lambda i: out[i].depart):
        leg = out[i]
        if leg.mode != "transit" or not now < leg.depart <= now + LOOKAHEAD:
            continue
        others = [*obstacles, *((o.depart, o.arrive) for j, o in enumerate(out) if j != i)]
        window = free_window(leg, others)
        if window is None:
            continue
        connection = await _connection(leg, window, buffer, now)
        if connection is None:
            continue
        tz = leg.depart.tzinfo
        out[i] = replace(
            leg,
            depart=connection.depart.astimezone(tz),
            arrive=connection.arrive.astimezone(tz),
            note="\n".join(filter(None, [describe(connection, tz), leg.note])),
        )
    return out


def free_window(leg: PlannedLeg, others: list[tuple[datetime, datetime]]) -> Window | None:
    """The end of whatever comes before the leg and the start of whatever
    comes after it. None when something overlaps the modelled leg: that's a
    conflict, and the conflict alert reports it."""
    earliest: datetime | None = None
    latest: datetime | None = None
    for start, end in others:
        if end <= leg.depart:
            earliest = end if earliest is None else max(earliest, end)
        elif start >= leg.arrive:
            latest = start if latest is None else min(latest, start)
        else:
            return None
    return earliest, latest


def fits(connection: TransitConnection, window: Window, slack: timedelta, now: datetime) -> bool:
    earliest, latest = window
    return (
        connection.depart >= now
        and (earliest is None or connection.depart >= earliest + slack)
        and (latest is None or connection.arrive <= latest - slack)
    )


def pick(
    after: TransitConnection | None,
    before: TransitConnection | None,
    window: Window,
    buffer: timedelta,
    now: datetime,
) -> TransitConnection | None:
    """`after` leaves at the modelled time, `before` arrives in time."""
    for connection, slack in ((after, buffer), (before, buffer), (before, timedelta(0)), (after, timedelta(0))):
        if connection is not None and fits(connection, window, slack, now):
            return connection
    return None


def describe(connection: TransitConnection, tz) -> str:
    boards = connection.boards.astimezone(tz).strftime("%H:%M")
    where = f" from {connection.stop}" if connection.stop else ""
    return f"{connection.line or 'Transit'}{where} at {boards}"


async def _connection(
    leg: PlannedLeg, window: Window, buffer: timedelta, now: datetime
) -> TransitConnection | None:
    after = await lookup(leg.origin, leg.destination, depart_at=leg.depart)
    if after is not None and fits(after, window, buffer, now):
        return after
    latest = window[1]
    before = None
    if latest is not None:
        before = await lookup(leg.origin, leg.destination, arrive_by=latest - buffer)
    return pick(after, before, window, buffer, now)


async def lookup(
    origin: str,
    destination: str,
    *,
    depart_at: datetime | None = None,
    arrive_by: datetime | None = None,
) -> TransitConnection | None:
    """Cached by the exact query: slot-aligned leg times repeat across
    solves. A failed call pauses lookups for a while so a missing Routes API
    or quota doesn't cost one failing request per leg on every solve."""
    global _backoff_until
    when = arrive_by or depart_at
    key = (origin, destination, "arrive" if arrive_by else "depart", int(when.timestamp()))
    clock = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > clock:
        return cached[1]
    if clock < _backoff_until:
        return None
    try:
        connection = await maps.transit_connection(
            origin=origin, destination=destination, depart_at=depart_at, arrive_by=arrive_by,
        )
    except (MapsLookupError, httpx.HTTPError) as exc:
        _backoff_until = clock + FAILURE_BACKOFF_SECONDS
        log.warning("transit · connection lookup failed, keeping modelled times err=%s", exc)
        return None
    for stale in [k for k, (expires, _) in _cache.items() if expires <= clock]:
        del _cache[stale]
    _cache[key] = (clock + CACHE_TTL_SECONDS, connection)
    return connection
