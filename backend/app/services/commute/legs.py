"""Pure commute-leg derivation.

An *anchor* is anything physical on the calendar — a fixed event or a
scheduled located task. Legs are derived from the ordered anchor timeline
and are never scheduled or repaired on their own: whenever an anchor moves,
its legs are recomputed from scratch here.

Chaining rules between consecutive anchors P → N:

  * same location            → no leg (already there)
  * gap fits a home layover  → P → home, then home → N
  * gap too small            → direct leg P → N

The bike lives at home: a leg may only be ridden when every previous leg
since the last home departure was ridden too. Once a chain leaves home by
transit (or any leg falls back to transit), the rest of that chain stays
off the bike until it passes through home again.

The module is pure: callers resolve route durations (`required_routes`
lists what's needed) and pass them in as a dict, so the derivation is
unit-testable from fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.timezones import to_user_tz

HOME = "home"

# When neither bike nor transit can route a leg, a placeholder of this length
# is written instead of silently reserving nothing — the user sees a visible
# "no route" block and still gets time to travel somehow.
FAILED_MODE = "failed"
FAILED_LEG_SECONDS = 30 * 60

Durations = dict[tuple[str, str, str], int | None]


@dataclass(frozen=True)
class Anchor:
    id: str
    start: datetime
    end: datetime
    location: str


@dataclass(frozen=True)
class PlannedLeg:
    origin_anchor: str
    dest_anchor: str
    origin: str
    destination: str
    mode: str
    depart: datetime
    arrive: datetime
    reason: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.origin_anchor, self.dest_anchor


def choose_mode(
    bike_seconds: int | None,
    rain_pct: int | None,
    settings,
    *,
    bike_available: bool = True,
) -> tuple[str, str | None]:
    if not bike_available:
        return "transit", "bike not along on this trip"
    if bike_seconds is None:
        return "transit", "bike unavailable"
    if bike_seconds > settings.commute_bike_max_minutes * 60:
        return "transit", "bike exceeds threshold"
    if rain_pct is not None and rain_pct >= settings.commute_rain_threshold_pct:
        return "transit", f"rain {rain_pct}% >= {settings.commute_rain_threshold_pct}% threshold"
    return "bicycling", None


def required_routes(anchors: list[Anchor], home: str) -> dict[tuple[str, str], datetime]:
    """Route pairs `derive_legs` may need, each with a reference departure
    time for the resolver's hour bucket."""
    ordered = sorted(anchors, key=lambda a: a.start)
    out: dict[tuple[str, str], datetime] = {}

    def _add(origin: str, destination: str, when: datetime) -> None:
        if _norm(origin) == _norm(destination):
            return
        out.setdefault((origin, destination), when)

    for anchor in ordered:
        _add(home, anchor.location, anchor.start)
        _add(anchor.location, home, anchor.end)
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        _add(prev.location, nxt.location, prev.end)
    return out


def derive_legs(
    anchors: list[Anchor],
    home: str,
    durations: Durations,
    rain: dict[str, int] | None,
    settings,
) -> tuple[list[PlannedLeg], int]:
    """Return `(legs, unroutable)` for the anchor timeline — `unroutable`
    counts the legs that got a `FAILED_MODE` 30-minute placeholder because
    no mode could route them."""
    ordered = [a for a in sorted(anchors, key=lambda x: x.start) if _norm(a.location) != _norm(home)]
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    layover = timedelta(minutes=settings.commute_home_layover_minutes)

    legs: list[PlannedLeg] = []
    # The bike lives at home: it's only on hand while every leg since the
    # last home departure was ridden. Any other mode strands it until the
    # timeline passes through home again.
    bike_with_me = True

    def _push(leg: PlannedLeg) -> None:
        nonlocal bike_with_me
        on_bike = leg.mode == "bicycling"
        bike_with_me = on_bike if leg.origin_anchor == HOME else (bike_with_me and on_bike)
        legs.append(leg)

    bounded: list[Anchor | None] = [None, *ordered, None]
    for prev, nxt in zip(bounded, bounded[1:], strict=False):
        if prev is None and nxt is None:
            continue
        if prev is None:
            _push(_arrive_leg(HOME, home, nxt, durations, rain, settings, buffer))
            continue
        if nxt is None:
            _push(_depart_leg(
                prev, HOME, home, durations, rain, settings, buffer,
                bike_available=bike_with_me,
            ))
            continue

        if _norm(prev.location) == _norm(nxt.location):
            continue
        gap = nxt.start - prev.end
        if gap <= timedelta(0):
            continue

        inbound = _leg_option(
            prev.location, home, prev.end, durations, rain, settings,
            bike_available=bike_with_me,
        )
        outbound = _leg_option(home, nxt.location, nxt.start, durations, rain, settings)
        if inbound is not None and outbound is not None:
            via_home_span = (
                buffer
                + timedelta(seconds=inbound[0])
                + layover
                + timedelta(seconds=outbound[0])
                + buffer
            )
            if gap >= via_home_span:
                _push(_depart_leg(
                    prev, HOME, home, durations, rain, settings, buffer,
                    bike_available=bike_with_me,
                ))
                _push(_arrive_leg(HOME, home, nxt, durations, rain, settings, buffer))
                continue

        direct = _leg_option(
            prev.location, nxt.location, nxt.start, durations, rain, settings,
            bike_available=bike_with_me,
        )
        if direct is None and inbound is not None and outbound is not None:
            # No direct route; go via home even though the layover is tight.
            _push(_depart_leg(
                prev, HOME, home, durations, rain, settings, buffer,
                bike_available=bike_with_me,
            ))
            _push(_arrive_leg(HOME, home, nxt, durations, rain, settings, buffer))
            continue
        # Routable → real direct leg; unroutable → 30-min failed placeholder.
        _push(
            _arrive_leg(
                prev.id, prev.location, nxt, durations, rain, settings, buffer,
                not_before=prev.end + buffer,
                bike_available=bike_with_me,
            )
        )
    return legs, sum(1 for leg in legs if leg.mode == FAILED_MODE)


def _arrive_leg(
    origin_anchor: str,
    origin: str,
    anchor: Anchor,
    durations: Durations,
    rain: dict[str, int] | None,
    settings,
    buffer: timedelta,
    *,
    not_before: datetime | None = None,
    bike_available: bool = True,
) -> PlannedLeg:
    option = _leg_option(
        origin, anchor.location, anchor.start, durations, rain, settings,
        bike_available=bike_available,
    )
    seconds, mode, reason = option if option is not None else _failed_option()
    arrive = anchor.start - buffer
    depart = arrive - timedelta(seconds=seconds)
    if not_before is not None and depart < not_before:
        # Physically late — anchor the leg to the earliest possible departure
        # and let the overlap machinery deal with what it collides with.
        depart = not_before
        arrive = depart + timedelta(seconds=seconds)
    return PlannedLeg(
        origin_anchor=origin_anchor,
        dest_anchor=anchor.id,
        origin=origin,
        destination=anchor.location,
        mode=mode,
        depart=depart,
        arrive=arrive,
        reason=reason,
    )


def _depart_leg(
    anchor: Anchor,
    dest_anchor: str,
    destination: str,
    durations: Durations,
    rain: dict[str, int] | None,
    settings,
    buffer: timedelta,
    *,
    bike_available: bool = True,
) -> PlannedLeg:
    option = _leg_option(
        anchor.location, destination, anchor.end, durations, rain, settings,
        bike_available=bike_available,
    )
    seconds, mode, reason = option if option is not None else _failed_option()
    depart = anchor.end + buffer
    return PlannedLeg(
        origin_anchor=anchor.id,
        dest_anchor=dest_anchor,
        origin=anchor.location,
        destination=destination,
        mode=mode,
        depart=depart,
        arrive=depart + timedelta(seconds=seconds),
        reason=reason,
    )


def _leg_option(
    origin: str,
    destination: str,
    when: datetime,
    durations: Durations,
    rain: dict[str, int] | None,
    settings,
    *,
    bike_available: bool = True,
) -> tuple[int, str, str | None] | None:
    """Chosen `(seconds, mode, reason)` for a leg, or None if unroutable."""
    bike = durations.get((origin, destination, "bicycling"))
    mode, reason = choose_mode(bike, _rain_at(rain, when), settings, bike_available=bike_available)
    if mode == "transit":
        transit = durations.get((origin, destination, "transit"))
        if transit is not None:
            return transit, "transit", reason
        if bike is None or not bike_available:
            return None
        return bike, "bicycling", "transit unavailable, fell back to bike"
    return bike, "bicycling", None


def _failed_option() -> tuple[int, str, str]:
    return FAILED_LEG_SECONDS, FAILED_MODE, "no route found"


def _rain_at(rain: dict[str, int] | None, when: datetime) -> int | None:
    if not rain:
        return None
    return rain.get(to_user_tz(when).strftime("%Y-%m-%dT%H:00"))


def _norm(location: str) -> str:
    return " ".join(location.lower().split())
