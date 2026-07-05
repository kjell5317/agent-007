"""Pure commute-leg derivation.

An *anchor* is anything physical on the calendar — a fixed event or a
scheduled located task. Legs are derived from the ordered anchor timeline
and are never scheduled or repaired on their own: whenever an anchor moves,
its legs are recomputed from scratch here.

Overlapping anchors (double-booked calendar) are grouped into a cluster
first: the arriving leg targets the cluster's earliest start, the departing
leg leaves after the cluster's *latest end* — no legs inside a cluster.

Chaining rules between consecutive clusters P → N:

  * same location            → no leg (already there)
  * gap fits a home layover  → P → home, then home → N
  * gap too small            → direct leg P → N

Legs slide to stay clear of `avoid` spans — online or location-less
events the timeline doesn't route to but the user still attends. Arriving
legs slide earlier (later means missing the anchor); departing legs slide
later (earlier means leaving mid-event). Both cap the shift at `MAX_DODGE`;
a bounded wait beats riding through a meeting, an unbounded one doesn't.

The bike lives at home and must come back home: a trip (home → … → home)
rides one mode. A leg may only be ridden when every previous leg since the
last home departure was ridden too, and a chain that would mix bike with
transit (e.g. rain flips only the return) is harmonized — transit for the
whole trip when it routes, otherwise the bike is kept throughout.

The module is pure: callers resolve route durations (`required_routes`
lists what's needed) and pass them in as a dict, so the derivation is
unit-testable from fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.timezones import to_user_tz

HOME = "home"

# When neither bike nor transit can route a leg, a placeholder of this length
# is written instead of silently reserving nothing — the user sees a visible
# "no route" block and still gets time to travel somehow.
FAILED_MODE = "failed"
FAILED_LEG_SECONDS = 30 * 60

# How far a leg may shift (arrivals earlier, departures later) to clear an
# avoid span. Beyond this the waiting costs more than the visible overlap
# it prevents, so the leg stays put. Legs between anchors are additionally
# bounded by the previous anchor's end.
MAX_DODGE = timedelta(hours=2)

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
    clusters = _overlap_clusters(_ordered_away(anchors, home))
    out: dict[tuple[str, str], datetime] = {}

    def _add(origin: str, destination: str, when: datetime) -> None:
        if _norm(origin) == _norm(destination):
            return
        out.setdefault((origin, destination), when)

    for cluster in clusters:
        entry, exit_ = _cluster_entry(cluster), _cluster_exit(cluster)
        _add(home, entry.location, entry.start)
        _add(exit_.location, home, exit_.end)
    for prev, nxt in zip(clusters, clusters[1:], strict=False):
        _add(_cluster_exit(prev).location, _cluster_entry(nxt).location, _cluster_exit(prev).end)
    return out


def _ordered_away(anchors: list[Anchor], home: str) -> list[Anchor]:
    return [a for a in sorted(anchors, key=lambda x: x.start) if _norm(a.location) != _norm(home)]


def _overlap_clusters(ordered: list[Anchor]) -> list[list[Anchor]]:
    """Group time-overlapping anchors — the user is double-booked, so no
    travel happens between them; the cluster is entered once and left once."""
    clusters: list[list[Anchor]] = []
    reach: datetime | None = None
    for anchor in ordered:
        if reach is not None and anchor.start < reach:
            clusters[-1].append(anchor)
            reach = max(reach, anchor.end)
        else:
            clusters.append([anchor])
            reach = anchor.end
    return clusters


def _cluster_entry(cluster: list[Anchor]) -> Anchor:
    return cluster[0]


def _cluster_exit(cluster: list[Anchor]) -> Anchor:
    return max(cluster, key=lambda a: a.end)


def derive_legs(
    anchors: list[Anchor],
    home: str,
    durations: Durations,
    rain: dict[str, int] | None,
    settings,
    avoid: list[tuple[datetime, datetime]] | None = None,
    missing_transit: set[tuple[str, str]] | None = None,
) -> tuple[list[PlannedLeg], int]:
    """Return `(legs, unroutable)` for the anchor timeline — `unroutable`
    counts the legs that got a `FAILED_MODE` 30-minute placeholder because
    no mode could route them. `avoid` spans (online meetings) push arriving
    legs earlier and departing legs later when the default placement would
    overlap them. `missing_transit` collects pairs whose transit duration
    the derivation wanted but was never resolved (see `_leg_option`)."""
    clusters = _overlap_clusters(_ordered_away(anchors, home))
    buffer = timedelta(minutes=settings.commute_event_buffer_minutes)
    layover = timedelta(minutes=settings.commute_home_layover_minutes)
    avoid = sorted(avoid or [])

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

    bounded: list[list[Anchor] | None] = [None, *clusters, None]
    for prev_cluster, nxt_cluster in zip(bounded, bounded[1:], strict=False):
        if prev_cluster is None and nxt_cluster is None:
            continue
        if prev_cluster is None:
            _push(_arrive_leg(
                HOME, home, _cluster_entry(nxt_cluster), durations, rain, settings, buffer,
                avoid=avoid, missing_transit=missing_transit,
            ))
            continue
        prev = _cluster_exit(prev_cluster)
        if nxt_cluster is None:
            _push(_depart_leg(
                prev, HOME, home, durations, rain, settings, buffer,
                bike_available=bike_with_me, missing_transit=missing_transit,
                avoid=avoid,
            ))
            continue
        nxt = _cluster_entry(nxt_cluster)

        if _norm(prev.location) == _norm(nxt.location):
            continue
        gap = nxt.start - prev.end
        if gap <= timedelta(0):
            continue

        inbound = _leg_option(
            prev.location, home, prev.end, durations, rain, settings,
            bike_available=bike_with_me, missing_transit=missing_transit,
        )
        outbound = _leg_option(
            home, nxt.location, nxt.start, durations, rain, settings,
            missing_transit=missing_transit,
        )
        if inbound is not None and outbound is not None:
            via_home_span = (
                buffer
                + timedelta(seconds=inbound[0])
                + layover
                + timedelta(seconds=outbound[0])
                + buffer
            )
            if gap >= via_home_span:
                home_leg = _depart_leg(
                    prev, HOME, home, durations, rain, settings, buffer,
                    bike_available=bike_with_me, missing_transit=missing_transit,
                    avoid=avoid,
                )
                _push(home_leg)
                _push(_arrive_leg(
                    HOME, home, nxt, durations, rain, settings, buffer,
                    not_before=home_leg.arrive,
                    avoid=avoid, missing_transit=missing_transit,
                ))
                continue

        direct = _leg_option(
            prev.location, nxt.location, nxt.start, durations, rain, settings,
            bike_available=bike_with_me, missing_transit=missing_transit,
        )
        if direct is None and inbound is not None and outbound is not None:
            # No direct route; go via home even though the layover is tight.
            _push(_depart_leg(
                prev, HOME, home, durations, rain, settings, buffer,
                bike_available=bike_with_me, missing_transit=missing_transit,
                avoid=avoid,
            ))
            _push(_arrive_leg(
                HOME, home, nxt, durations, rain, settings, buffer,
                missing_transit=missing_transit,
            ))
            continue
        # Routable → real direct leg; unroutable → 30-min failed placeholder.
        _push(
            _arrive_leg(
                prev.id, prev.location, nxt, durations, rain, settings, buffer,
                not_before=prev.end + buffer,
                bike_available=bike_with_me,
                avoid=avoid,
                missing_transit=missing_transit,
            )
        )
    legs = _harmonize_chain_modes(legs, durations, settings, missing_transit)
    return legs, sum(1 for leg in legs if leg.mode == FAILED_MODE)


def _harmonize_chain_modes(
    legs: list[PlannedLeg],
    durations: Durations,
    settings,
    missing_transit: set[tuple[str, str]] | None,
) -> list[PlannedLeg]:
    """A trip rides one mode — the bike must come back home.

    A chain mixing bike and transit (e.g. rain flips only the return)
    strands the bike, so the whole trip goes onto transit. When some leg
    has no transit route the trip keeps the bike throughout instead — a
    wet ride home beats a stranded bike."""
    out: list[PlannedLeg] = []
    for chain in _chains(legs):
        out.extend(_harmonized_chain(chain, durations, settings, missing_transit))
    return out


def _chains(legs: list[PlannedLeg]) -> list[list[PlannedLeg]]:
    chains: list[list[PlannedLeg]] = []
    current: list[PlannedLeg] = []
    for leg in legs:
        if leg.origin_anchor == HOME and current:
            chains.append(current)
            current = []
        current.append(leg)
    if current:
        chains.append(current)
    return chains


def _harmonized_chain(
    chain: list[PlannedLeg],
    durations: Durations,
    settings,
    missing_transit: set[tuple[str, str]] | None,
) -> list[PlannedLeg]:
    bike_legs = [leg for leg in chain if leg.mode == "bicycling"]
    others = [leg for leg in chain if leg.mode != "bicycling"]
    if not bike_legs or not others:
        return chain

    # Preferred: the whole trip on transit.
    transit: dict[tuple[str, str], int | None] = {}
    unfetched = False
    for leg in bike_legs:
        key = (leg.origin, leg.destination, "transit")
        if key not in durations:
            if missing_transit is not None:
                missing_transit.add((leg.origin, leg.destination))
            unfetched = True
        else:
            transit[leg.key] = durations[key]
    if unfetched:
        # The caller fetches the recorded pairs and re-derives.
        return chain
    if all(seconds is not None for seconds in transit.values()):
        return [
            _with_mode(leg, "transit", transit[leg.key], "one mode per trip — transit throughout")
            if leg.mode == "bicycling"
            else leg
            for leg in chain
        ]

    # No full transit trip — ride the whole way if every flipped leg can be
    # ridden (rain flips can; threshold or no-route legs cannot, and then
    # the mix stays visible for the user to resolve).
    bike: dict[tuple[str, str], int] = {}
    for leg in others:
        seconds = durations.get((leg.origin, leg.destination, "bicycling"))
        if (
            leg.mode != "transit"
            or seconds is None
            or seconds > settings.commute_bike_max_minutes * 60
        ):
            return chain
        bike[leg.key] = seconds
    return [
        _with_mode(leg, "bicycling", bike[leg.key], "no transit for the whole trip — keeping the bike")
        if leg.mode != "bicycling"
        else leg
        for leg in chain
    ]


def _with_mode(leg: PlannedLeg, mode: str, seconds: int, reason: str) -> PlannedLeg:
    """Swap a leg's mode keeping its anchored end fixed: arrivals stay
    anchored on `arrive`, rides home on `depart`."""
    if leg.dest_anchor == HOME:
        return replace(leg, mode=mode, reason=reason, arrive=leg.depart + timedelta(seconds=seconds))
    return replace(leg, mode=mode, reason=reason, depart=leg.arrive - timedelta(seconds=seconds))


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
    avoid: list[tuple[datetime, datetime]] | None = None,
    missing_transit: set[tuple[str, str]] | None = None,
) -> PlannedLeg:
    option = _leg_option(
        origin, anchor.location, anchor.start, durations, rain, settings,
        bike_available=bike_available, missing_transit=missing_transit,
    )
    seconds, mode, reason = option if option is not None else _failed_option()
    arrive = anchor.start - buffer
    depart = arrive - timedelta(seconds=seconds)
    if avoid:
        # The avoided events aren't this leg's anchors, so the full event
        # gap applies — not the inner leg↔anchor buffer.
        clearance = timedelta(minutes=settings.event_buffer_minutes)
        depart, arrive = _dodged_earlier(depart, arrive, avoid, clearance, floor=not_before)
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
    missing_transit: set[tuple[str, str]] | None = None,
    avoid: list[tuple[datetime, datetime]] | None = None,
) -> PlannedLeg:
    option = _leg_option(
        anchor.location, destination, anchor.end, durations, rain, settings,
        bike_available=bike_available, missing_transit=missing_transit,
    )
    seconds, mode, reason = option if option is not None else _failed_option()
    depart = anchor.end + buffer
    arrive = depart + timedelta(seconds=seconds)
    if avoid:
        # E.g. an online meeting right after the anchor: attend it there and
        # ride afterwards, keeping the full event gap — it isn't this leg's
        # anchor. Departing earlier would mean leaving mid-event, so the
        # only direction is later.
        clearance = timedelta(minutes=settings.event_buffer_minutes)
        depart, arrive = _dodged_later(depart, arrive, avoid, clearance)
    return PlannedLeg(
        origin_anchor=anchor.id,
        dest_anchor=dest_anchor,
        origin=anchor.location,
        destination=destination,
        mode=mode,
        depart=depart,
        arrive=arrive,
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
    missing_transit: set[tuple[str, str]] | None = None,
) -> tuple[int, str, str | None] | None:
    """Chosen `(seconds, mode, reason)` for a leg, or None if unroutable.

    A transit duration that was never resolved (key absent, as opposed to a
    definitive no-route None) is recorded in `missing_transit` so the caller
    can fetch it and re-derive — this only happens when the bike-stays-home
    rule forces transit on a pair the resolver had no reason to fetch."""
    bike = durations.get((origin, destination, "bicycling"))
    mode, reason = choose_mode(bike, _rain_at(rain, when), settings, bike_available=bike_available)
    if mode == "transit":
        key = (origin, destination, "transit")
        transit = durations.get(key)
        if transit is not None:
            return transit, "transit", reason
        if key not in durations and missing_transit is not None:
            missing_transit.add((origin, destination))
        if bike is None or not bike_available:
            return None
        return bike, "bicycling", "transit unavailable, fell back to bike"
    return bike, "bicycling", None


def _dodged_earlier(
    depart: datetime,
    arrive: datetime,
    avoid: list[tuple[datetime, datetime]],
    clearance: timedelta,
    *,
    floor: datetime | None,
) -> tuple[datetime, datetime]:
    """Slide `[depart, arrive]` earlier until it clears every avoid span by
    `clearance`. Falls back to the original placement when nothing at or
    above `floor` (or the `MAX_DODGE` cap) clears — the overlap is
    then at least visible on the calendar."""
    span = arrive - depart
    original = (depart, arrive)
    cap = depart - MAX_DODGE
    floor = max(floor, cap) if floor is not None else cap
    for _ in range(len(avoid) + 1):
        conflict = max(
            (a for a in avoid if a[0] < arrive + clearance and depart - clearance < a[1]),
            key=lambda a: a[0],
            default=None,
        )
        if conflict is None:
            return depart, arrive
        arrive = conflict[0] - clearance
        depart = arrive - span
        if depart < floor:
            return original
    return original


def _dodged_later(
    depart: datetime,
    arrive: datetime,
    avoid: list[tuple[datetime, datetime]],
    clearance: timedelta,
) -> tuple[datetime, datetime]:
    """Slide `[depart, arrive]` later until it clears every avoid span by
    `clearance`. Falls back to the original placement when the shift would
    exceed `MAX_DODGE` — the overlap then stays visible on the calendar."""
    span = arrive - depart
    original = (depart, arrive)
    ceiling = depart + MAX_DODGE
    for _ in range(len(avoid) + 1):
        conflict = min(
            (a for a in avoid if a[0] < arrive + clearance and depart - clearance < a[1]),
            key=lambda a: a[1],
            default=None,
        )
        if conflict is None:
            return depart, arrive
        depart = conflict[1] + clearance
        arrive = depart + span
        if depart > ceiling:
            return original
    return original


def _failed_option() -> tuple[int, str, str]:
    return FAILED_LEG_SECONDS, FAILED_MODE, "no route found"


def _rain_at(rain: dict[str, int] | None, when: datetime) -> int | None:
    if not rain:
        return None
    return rain.get(to_user_tz(when).strftime("%Y-%m-%dT%H:00"))


def _norm(location: str) -> str:
    return " ".join(location.lower().split())
