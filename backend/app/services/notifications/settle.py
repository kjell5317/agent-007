"""Send state notifications only once scheduling has settled.

A solve is often followed by more within seconds or minutes: a calendar
webhook echoing the solver's own writes, the routing retry. Warnings sent
after every pass got revoked by the next one, so every finished solve
re-arms a quiet-period timer instead; once no solve has run or queued for
`SETTLE_SECONDS`, the reconciler diffs what should show against the ledger.
An hourly tick catches deadlines that come closer with the clock alone.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import notification_ledger as ledger
from app.db.clients import tasks as tasks_store
from app.services.commute.legs import RoutingIssues
from app.services.notifications import push, reconcile
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

SETTLE_SECONDS = 90

_generation = 0
_routing: RoutingIssues | None = None
# Strong references: the loop only keeps weak ones to running tasks.
_timers: set[asyncio.Task] = set()


def solve_finished(routing: RoutingIssues | None) -> None:
    """`routing` is None when the solve wrote no legs; the last known issues stand."""
    global _generation, _routing
    if routing is not None:
        _routing = routing
    _generation += 1
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    timer = loop.create_task(_after_quiet(_generation))
    _timers.add(timer)
    timer.add_done_callback(_timers.discard)


async def _after_quiet(generation: int) -> None:
    await asyncio.sleep(SETTLE_SECONDS)
    if generation != _generation:
        return
    try:
        await reconcile_now()
    except Exception:  # noqa: BLE001 — the hourly tick retries
        log.exception("notifications · reconcile after solve failed")


async def reconcile_now() -> dict[str, int] | None:
    """None when a solve is running or queued — its finish re-arms the timer."""
    from app.db import SessionLocal
    from app.services.plan.replan import busy, routing_retry_pending

    if busy():
        return None
    with SessionLocal() as session:
        return await reconcile_session(
            session, routing=None if routing_retry_pending() else _routing
        )


async def reconcile_session(session: Session, *, routing: RoutingIssues | None) -> dict[str, int]:
    """Commute kinds are left as they are when `routing` is None: unknown
    isn't resolved."""
    now = datetime.now(user_tz())
    desired = reconcile.past_deadline(task_states(session), now)
    kinds = [reconcile.PAST_DEADLINE]
    if routing is not None:
        desired += reconcile.commute(routing)
        kinds += list(reconcile.COMMUTE_KINDS)
    plan = reconcile.plan(desired, ledger.of_kinds(session, kinds))
    stamp = datetime.now(timezone.utc)
    for item in plan.send:
        await push.send(item.message)
        ledger.record_sent(
            session, key=item.key, kind=item.kind, tag=item.tag,
            fingerprint=item.fingerprint, task_id=item.task_id, now=stamp,
        )
    for row in plan.clear:
        await push.clear(row.tag)
        ledger.record_cleared(session, row, stamp)
    session.commit()
    if plan.send or plan.clear:
        log.info("notifications · reconciled sent=%d cleared=%d", len(plan.send), len(plan.clear))
    return {"sent": len(plan.send), "cleared": len(plan.clear)}


def task_states(session: Session) -> list[reconcile.TaskState]:
    from app.services.plan.cue import booked_slot

    out: list[reconcile.TaskState] = []
    for task in tasks_store.open_schedulable(session):
        if task.due_date is None:
            continue
        slot = booked_slot(task) if task.schedule_state == "placed" else None
        out.append(reconcile.TaskState(
            id=task.id,
            title=task.title or "",
            due=to_user_tz(task.due_date),
            deadline_kind=task.deadline_kind or "hard",
            schedule_state=task.schedule_state,
            slot=slot,
            running=task.running_since is not None,
            waiting=task.schedule_state == "waiting",
        ))
    return out
