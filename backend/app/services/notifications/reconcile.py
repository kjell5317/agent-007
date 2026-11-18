"""Which state notifications should be showing, and the send/clear diff
against the ledger — pure."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.services.commute.legs import HOME, PlannedLeg, RoutingIssues, _norm
from app.services.notifications import messages
from app.services.notifications.push import Message

PAST_DEADLINE = "past_deadline"
COMMUTE_CONFLICT = "commute_conflict"
COMMUTE_NO_ROUTE = "commute_no_route"
COMMUTE_KINDS = (COMMUTE_CONFLICT, COMMUTE_NO_ROUTE)

# Later holds only tasks due past the 7-day horizon; one still there once its
# deadline is closer than that won't get a slot in time.
LATER_WINDOW = timedelta(days=7)
# A deadline that passed longer ago than this isn't news: it was announced
# when it happened, or before this rule existed.
PASSED_NEWS = timedelta(days=1)


@dataclass(frozen=True)
class TaskState:
    id: uuid.UUID
    title: str
    due: datetime
    deadline_kind: str
    schedule_state: str
    slot: tuple[datetime, datetime] | None
    running: bool = False
    waiting: bool = False


@dataclass(frozen=True)
class Desired:
    key: str
    kind: str
    fingerprint: str
    message: Message
    task_id: uuid.UUID | None = None
    # Never re-sent for the same fingerprint, even after it was cleared.
    once: bool = False
    # False: keep it if it is already showing, but don't send it now.
    news: bool = True

    @property
    def tag(self) -> str:
        return self.message.tag or ""


@dataclass(frozen=True)
class Plan:
    send: list[Desired]
    clear: list  # ledger rows


def deadline_case(task: TaskState, now: datetime) -> str | None:
    if task.due <= now:
        return "passed"
    if task.waiting:
        return None
    if task.schedule_state == "unplaced":
        return "unplaced"
    if task.schedule_state == "deferred" and task.due - now < LATER_WINDOW:
        return "later"
    if task.schedule_state == "placed" and task.slot is not None and task.slot[1] > task.due:
        return "late"
    return None


def past_deadline(tasks: list[TaskState], now: datetime) -> list[Desired]:
    out: list[Desired] = []
    for task in tasks:
        case = deadline_case(task, now)
        if case is None:
            continue
        out.append(Desired(
            key=f"deadline:{task.id}",
            kind=PAST_DEADLINE,
            fingerprint=f"{task.due.isoformat()}|{task.deadline_kind}",
            message=messages.past_deadline(
                task_id=task.id, title=task.title, due=task.due,
                deadline_kind=task.deadline_kind, case=case, slot=task.slot,
            ),
            task_id=task.id,
            # While it's being worked on a warning isn't news, but one that is
            # showing stays — clearing it would re-send it at the next pause.
            news=not task.running and (case != "passed" or now - task.due < PASSED_NEWS),
        ))
    return out


def event_side(leg: PlannedLeg) -> tuple[str, str]:
    """The event a leg to or from home serves, and its address: the trip there
    and the trip back belong to the same event."""
    if leg.dest_anchor != HOME:
        return leg.dest_anchor, leg.destination
    return leg.origin_anchor, leg.origin


def commute(issues: RoutingIssues) -> list[Desired]:
    out: list[Desired] = []
    unroutable = sorted(issues.unroutable, key=lambda leg: leg.depart)
    # A failing trip from or to home pins the bad address on that event.
    blamed: dict[str, tuple[str, datetime]] = {}
    for leg in unroutable:
        if HOME in leg.key:
            anchor, address = event_side(leg)
            blamed.setdefault(anchor, (address, leg.depart))
    for anchor, (address, depart) in blamed.items():
        out.append(Desired(
            key=f"noroute:{anchor}",
            kind=COMMUTE_NO_ROUTE,
            fingerprint=_norm(address),
            message=messages.no_route(tag=f"noroute-{anchor}", address=address, depart=depart),
            once=True,
        ))
    # Between two events neither of which is known bad, either address may be
    # wrong: name the trip.
    for leg in unroutable:
        a, b = leg.key
        if HOME in leg.key or a in blamed or b in blamed:
            continue
        out.append(Desired(
            key=f"noroute:{a}:{b}",
            kind=COMMUTE_NO_ROUTE,
            fingerprint=f"{_norm(leg.origin)}|{_norm(leg.destination)}",
            message=messages.no_route(
                tag=f"noroute-{a}-{b}", address=f"{leg.origin} → {leg.destination}", depart=leg.depart,
            ),
            once=True,
        ))
    for leg, blocker_id, blocker_label in issues.conflicts:
        a, b = leg.key
        out.append(Desired(
            key=f"conflict:{a}:{b}",
            kind=COMMUTE_CONFLICT,
            fingerprint=blocker_id,
            message=messages.leg_conflict(
                tag=f"conflict-{a}-{b}", destination=leg.destination,
                blocker=blocker_label, depart=leg.depart,
            ),
        ))
    return out


def plan(desired: list[Desired], ledger_rows: list) -> Plan:
    """`ledger_rows` are every row of the kinds `desired` covers, cleared ones
    included — `once` needs to know what was ever sent."""
    rows = {row.key: row for row in ledger_rows}
    wanted = {d.key for d in desired}
    send: list[Desired] = []
    for d in desired:
        row = rows.get(d.key)
        if row is not None and row.fingerprint == d.fingerprint:
            if row.cleared_at is None or d.once:
                continue
        if not d.news:
            continue
        send.append(d)
    clear = [row for row in ledger_rows if row.cleared_at is None and row.key not in wanted]
    return Plan(send=send, clear=clear)
