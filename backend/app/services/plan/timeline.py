"""What surrounds a slot — pure helpers shared by the solver input, the
feature snapshots in the schedule log and the user-placement anchor.

The grid runs on local wall-clock time: slot `t` is `t * 5` minutes after
local midnight of the horizon's first day, so `t % 288` is always the
time of day even across DST changes.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from app.services.plan import features as F
from app.services.plan.solver import SLOT_MINUTES, SLOTS_PER_DAY

ANCHOR_RADIUS = timedelta(hours=2)
# A day-load shift beyond this releases a hand-placed task's anchor.
ANCHOR_LOAD_MINUTES = 60


@dataclass(frozen=True)
class Item:
    start: datetime
    end: datetime
    kind: str  # "event" | "task"
    label: str | None = None
    work_type: str | None = None
    effort: str | None = None
    task_id: uuid.UUID | None = None
    event_id: str | None = None
    # Routable place, None for online or location-less items.
    location: str | None = None

    @property
    def ref(self) -> str:
        return str(self.task_id) if self.task_id else f"event:{self.event_id}"


@dataclass(frozen=True)
class Grid:
    origin: date
    days: int
    tz: tzinfo

    @property
    def horizon(self) -> int:
        return self.days * SLOTS_PER_DAY

    @property
    def dates(self) -> list[date]:
        return [self.origin + timedelta(days=d) for d in range(self.days)]

    def _minutes(self, dt: datetime) -> float:
        local = dt.astimezone(self.tz)
        day = (local.date() - self.origin).days
        return day * 1440 + local.hour * 60 + local.minute + local.second / 60

    def floor(self, dt: datetime) -> int:
        return math.floor(self._minutes(dt) / SLOT_MINUTES)

    def ceil(self, dt: datetime) -> int:
        return math.ceil(self._minutes(dt) / SLOT_MINUTES)

    def at(self, slot: int) -> datetime:
        day, minute = divmod(slot * SLOT_MINUTES, 1440)
        return datetime.combine(
            self.origin + timedelta(days=day), time(minute // 60, minute % 60), tzinfo=self.tz
        )


def merged_spans(items: list[Item]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for item in sorted(items, key=lambda i: i.start):
        if out and item.start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], item.end))
        else:
            out.append((item.start, item.end))
    return out


def neighbours(
    items: list[Item], start: datetime, end: datetime, *, exclude: str | None = None
) -> tuple[F.Neighbour | None, F.Neighbour | None]:
    """Nearest item ending at or before `start` and nearest starting at or
    after `end`. Items overlapping the slot are not neighbours."""
    prev: Item | None = None
    nxt: Item | None = None
    for item in items:
        if exclude is not None and item.ref == exclude:
            continue
        if item.end <= start and (prev is None or item.end > prev.end):
            prev = item
        elif item.start >= end and (nxt is None or item.start < nxt.start):
            nxt = item

    def as_neighbour(item: Item | None, gap: timedelta) -> F.Neighbour | None:
        if item is None:
            return None
        return F.Neighbour(
            kind=item.kind, gap_min=gap.total_seconds() / 60, label=item.label,
            work_type=item.work_type, effort=item.effort,
        )

    return (
        as_neighbour(prev, start - prev.end) if prev else None,
        as_neighbour(nxt, nxt.start - end) if nxt else None,
    )


def item_load_on(item: Item, day: date, tz: tzinfo) -> float:
    """Booked minutes `item` contributes to `day`: events by their overlap
    with the day, tasks entirely on their start day (matching the solver)."""
    if item.kind == "task":
        return (item.end - item.start).total_seconds() / 60 if item.start.astimezone(tz).date() == day else 0.0
    lo = datetime.combine(day, time.min, tzinfo=tz)
    hi = lo + timedelta(days=1)
    overlap = min(item.end, hi) - max(item.start, lo)
    return max(0.0, overlap.total_seconds() / 60)


def day_load(items: list[Item], day: date, tz: tzinfo, *, exclude: str | None = None) -> float:
    return sum(item_load_on(i, day, tz) for i in items if exclude is None or i.ref != exclude)


def meeting_minutes(items: list[Item], day: date, tz: tzinfo) -> float:
    """Minutes of social events (meetings) on `day`."""
    return sum(
        item_load_on(i, day, tz) for i in items if i.kind == "event" and i.work_type == "social"
    )


@dataclass(frozen=True)
class TaskAttrs:
    task_id: uuid.UUID
    due: datetime
    deadline_kind: str
    label: str | None
    work_type: str | None
    effort: str | None


def slot_context(
    attrs: TaskAttrs,
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    items: list[Item],
    tz: tzinfo,
) -> F.SlotContext:
    ref = str(attrs.task_id)
    prev, nxt = neighbours(items, start, end, exclude=ref)
    local_start = start.astimezone(tz)
    load = day_load(items, local_start.date(), tz, exclude=ref) + (end - start).total_seconds() / 60
    return F.SlotContext(
        start=local_start,
        end=end.astimezone(tz),
        now=now.astimezone(tz),
        due=attrs.due.astimezone(tz),
        deadline_kind=attrs.deadline_kind,
        label=attrs.label,
        work_type=attrs.work_type,
        effort=attrs.effort,
        prev=prev,
        next=nxt,
        day_load_min=load,
        meeting_min=meeting_minutes(items, local_start.date(), tz),
    )


def slot_features(attrs: TaskAttrs, start: datetime, end: datetime, **kw) -> dict[str, float]:
    return F.features(slot_context(attrs, start, end, **kw))


# --- Anchor of a hand-placed task ---------------------------------------------


def placement_context(
    task_id: uuid.UUID, start: datetime, end: datetime, *, items: list[Item], tz: tzinfo
) -> dict:
    """Items within ±2 h of a slot and the day's load — stored when the user
    places a task, compared later to tell whether its surroundings changed."""
    ref = str(task_id)
    lo, hi = start - ANCHOR_RADIUS, end + ANCHOR_RADIUS
    near = sorted(
        [i.ref, i.start.isoformat(), i.end.isoformat()]
        for i in items
        if i.ref != ref and i.start < hi and i.end > lo
    )
    load = day_load(items, start.astimezone(tz).date(), tz, exclude=ref)
    return {"near": near, "day_load": round(load), "slot": [start.isoformat(), end.isoformat()]}


def surroundings_unchanged(stored: dict | None, current: dict) -> bool:
    if not stored:
        return False
    if stored.get("slot") != current.get("slot"):
        return False
    if stored.get("near") != current.get("near"):
        return False
    return abs(float(stored.get("day_load", 0)) - float(current.get("day_load", 0))) <= ANCHOR_LOAD_MINUTES


# --- Free time in good hours (deferral capacity) ------------------------------

# An hour is "good" when its start- and end-hour weights together cost less
# than this — under the prior, 10:00–19:00.
GOOD_HOUR_FLOOR = -0.5


def good_hours(weights: dict[str, float]) -> set[int]:
    return {
        h for h in range(24)
        if weights.get(f"h_start:{h}", 0.0) + weights.get(f"h_end:{h}", 0.0) >= GOOD_HOUR_FLOOR
    }


def free_good_minutes(
    items: list[Item], first: date, last: date, hours: set[int], tz: tzinfo
) -> dict[date, float]:
    """Per day in `[first, last]`, minutes of good hours not covered by items."""
    busy = merged_spans(items)
    out: dict[date, float] = {}
    day = first
    while day <= last:
        free = 0.0
        for hour in sorted(hours):
            lo = datetime.combine(day, time(hour), tzinfo=tz)
            hi = lo + timedelta(hours=1)
            covered = sum(
                max(0.0, (min(e, hi) - max(s, lo)).total_seconds() / 60)
                for s, e in busy if s < hi and e > lo
            )
            free += max(0.0, 60 - covered)
        out[day] = free
        day += timedelta(days=1)
    return out
