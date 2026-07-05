"""Discover externally-modified calendar events.

Incremental sync via Google's `syncToken`: each poll asks "what changed since
last time?" and Google answers with edited AND cancelled events. Three things
happen to the results on the write calendar:

  1. Task events that were edited (moved, renamed, re-timed, relocated,
     re-described) sync their new state back into the task row.
  2. Task events that were deleted get their task re-planned onto a fresh
     slot, blocking the one they were removed from.
  3. Any updated event that now overlaps a managed event hands off to
     `services.plan.reschedule.reschedule_event` to resolve the conflict.

The `syncToken` is stored per calendar on the oauth_tokens row (extra JSON).
A full baseline sync is re-run at most once a day so the look-ahead window
slides forward and recurring-event expansion stays bounded; between baselines
the stored token drives cheap incremental pulls.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, time as dt_time, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import oauth_tokens
from app.db.models.task import Task
from app.events import publish_task
from app.services.calendar.client import (
    CalendarEvent,
    CalendarSyncTokenExpired,
    _parse_time,
    authorized_client,
    normalize,
)
from app.services.calendar.events import (
    WINDOW_DAYS,
    _task_description,
    is_commute_event,
    is_managed_event,
    is_task_event,
)
from app.services.location import resolve_location_alias
from app.services.plan.schedule import (
    _duration_minutes,
    reschedule_event,
    schedule_task,
    scheduled_interval_for,
)

log = logging.getLogger(__name__)

# Cursor keys inside oauth_tokens.extra.
_SYNC_TOKENS_KEY = "calendar_sync_tokens"
_BASELINES_KEY = "calendar_sync_baselines"

# Re-run a full baseline sync (sliding the look-ahead window) at most this
# often; incremental syncToken pulls handle everything in between.
REBASELINE_INTERVAL = timedelta(days=1)


async def discover_updated_events(
    session: Session,
    *,
    calendar_ids: Iterable[str],
    account_key: str | None = None,
) -> dict:
    """Fetch events changed since the last poll and reconcile them.

    Returns a summary dict:
      * `checked`            — managed write events seen in the look-ahead window
      * `updated`            — active events changed since the last sync
      * `overlapping`        — updated events that triggered a reschedule call
      * `scheduled_updates`  — task events whose edits synced back to the row
      * `deleted`            — deleted task events re-planned onto a new slot
    """
    settings = get_settings()
    write_id = (settings.google_calendar_id or "").strip()
    ids = _calendar_ids(calendar_ids)
    if not ids or not write_id:
        return _empty_summary()

    token_row = oauth_tokens.get_decrypted(session, provider="google", account_key=account_key)
    if token_row is None:
        return _empty_summary()

    now = datetime.now(timezone.utc)
    # Look one day back to catch in-progress events whose start drifted earlier,
    # and WINDOW_DAYS forward so an updated event has neighbours to compare to.
    window_start = now - timedelta(days=1)
    window_end = now + timedelta(days=WINDOW_DAYS)

    extra = token_row.extra or {}
    sync_tokens = dict(extra.get(_SYNC_TOKENS_KEY) or {})
    baselines = dict(extra.get(_BASELINES_KEY) or {})

    client = await authorized_client(session, account_key)

    summary: dict = {
        "checked": 0,
        "updated": 0,
        "overlapping": 0,
        "scheduled_updates": 0,
        "deleted": 0,
    }

    changed_physical: list[CalendarEvent] = []
    deleted_spans: list[tuple[datetime, datetime]] = []
    for cid in ids:
        raw_items, new_token, new_baseline = await _sync_calendar(
            client,
            cid,
            token=sync_tokens.get(cid),
            baseline_iso=baselines.get(cid),
            now=now,
            window_start=window_start,
            window_end=window_end,
        )
        if new_token is not None:
            sync_tokens[cid] = new_token
        elif cid in sync_tokens:
            # Sync bailed without a token (page cap, see _MAX_SYNC_PAGES) — a
            # single change-set too large to page through, e.g. a huge recurring
            # series deleted at once. Drop the stale cursor so the next run
            # re-baselines within the window instead of replaying the same
            # overflowing delta forever.
            del sync_tokens[cid]
            log.info("discover · sync overflowed for %s; dropping token to re-baseline", cid)
        baselines[cid] = new_baseline.isoformat()

        active = _active_events(raw_items, cid)
        cancelled_items = [
            it for it in raw_items if it.get("status") == "cancelled" and it.get("id")
        ]
        summary["updated"] += len(active)
        changed_physical.extend(active)

        if cid == write_id:
            synced_task_ids: list[uuid.UUID] = []
            for ev in active:
                if not is_task_event(ev):
                    continue
                synced_task_id = _sync_task_schedule_from_event(session, ev)
                if synced_task_id is not None:
                    synced_task_ids.append(synced_task_id)
                    summary["scheduled_updates"] += 1
            session.commit()
            for task_id in synced_task_ids:
                publish_task(session, task_id)

        skipped_out_of_window = 0
        for item in cancelled_items:
            if cid == write_id:
                rescheduled = await _reschedule_deleted_task_event(
                    session, item["id"], account_key=account_key,
                )
                if rescheduled is not None:
                    summary["deleted"] += 1
                    publish_task(session, rescheduled)
                    continue
            # Any other deletion — online meeting, located event, even a
            # manually-removed leg — changes what the legs around it should
            # look like. The sync tombstone carries no times, so recover the
            # span and replan that window.
            span = await _cancelled_event_span(session, cid, item, account_key=account_key)
            if span is None:
                continue
            # Incremental sync is unbounded in time: deleting a long recurring
            # series streams back tombstones for instances years outside the
            # look-ahead window. Only deletions that touch the window can move
            # commutes we actually plan, so drop the rest — a bulk series
            # deletion is thousands of these, so count them and log once.
            if not _span_touches_window(span, window_start, window_end):
                skipped_out_of_window += 1
                continue
            log.info(
                "discover · event id=%s deleted on %s; replanning commutes around %s..%s",
                item["id"], cid, span[0].isoformat(), span[1].isoformat(),
            )
            deleted_spans.append(span)
        if skipped_out_of_window:
            log.info(
                "discover · %d deleted event(s) on %s outside window %s..%s; skipped",
                skipped_out_of_window, cid, window_start.isoformat(), window_end.isoformat(),
            )

        # Something changed in a read/busy calendar, or a manual event on the
        # write calendar. Pull the write calendar and only move events we own
        # there; read events and manual write events are hard blockers.
        items = await client.list_events(write_id, time_min=window_start, time_max=window_end)
        write_events = _active_events(items, write_id)
        summary["checked"] += len(write_events)

        event_gap = timedelta(minutes=settings.event_buffer_minutes)
        for ev in active:
            if cid == write_id and is_managed_event(ev):
                continue
            # A single changed event can collide with several tasks and legs
            # at once — every one within the event gap needs its own reschedule.
            for overlapping in _managed_conflicts(ev, write_events, event_gap):
                summary["overlapping"] += 1
                kind = (
                    "commute" if is_commute_event(overlapping)
                    else "task" if is_task_event(overlapping)
                    else "event"
                )
                log.info(
                    "discover · %r (%s, id=%s) conflicts with %s %r (%s); rescheduling",
                    ev.summary, ev.start.isoformat(), ev.id,
                    kind, overlapping.summary, overlapping.start.isoformat(),
                )
                await reschedule_event(
                    session,
                    overlapping.id,
                    account_key=account_key,
                )

    # Persist cursors only after a successful pass. If anything above raised
    # we'll re-check with the same tokens next run, which keeps overlap and
    # deletion handling at-least-once.
    oauth_tokens.set_extra(
        session,
        provider="google",
        account_key=token_row.account_key,
        patch={_SYNC_TOKENS_KEY: sync_tokens, _BASELINES_KEY: baselines},
    )
    session.commit()

    await _plan_legs_for_changed_events(
        session, changed_physical, deleted_spans=deleted_spans, account_key=account_key,
    )

    return summary


def _span_touches_window(
    span: tuple[datetime, datetime],
    window_start: datetime,
    window_end: datetime,
) -> bool:
    return span[1] >= window_start and span[0] <= window_end


async def _cancelled_event_span(
    session: Session,
    calendar_id: str,
    item: dict,
    *,
    account_key: str | None,
) -> tuple[datetime, datetime] | None:
    """Time span of a deleted event, or None when it can't be recovered.

    Sync tombstones are bare `{id, status}`; cancelled recurring instances
    additionally carry `originalStartTime` and encode their original start in
    the id (`{master}_{start}`). We recover the time offline from either — a
    bulk series deletion is thousands of tombstones, and `events.get` on an
    instance can hand back the master (dated at the series origin), so hitting
    the API per instance would be both wasteful and wrong. `get_event` is a
    last resort for plain, non-recurring deletions."""
    original = item.get("originalStartTime")
    if original:
        start, _ = _parse_time(original)
        return start, start
    instance_start = _recurring_instance_start(item["id"])
    if instance_start is not None:
        return instance_start, instance_start
    from app.services.calendar import get_event

    try:
        event = await get_event(
            session, calendar_id=calendar_id, event_id=item["id"], account_key=account_key,
        )
    except Exception as exc:  # noqa: BLE001 — tombstone may be gone or timeless
        log.info(
            "discover · deleted event=%s span unrecoverable (%s); daily re-baseline will heal",
            item["id"], exc,
        )
        return None
    return event.start, event.end


def _recurring_instance_start(event_id: str) -> datetime | None:
    """The original UTC start encoded in a recurring-instance id, or None.

    Google names expanded instances `{masterId}_{start}`, where `start` is the
    instance's original start in basic-ISO UTC — `20380513T110000Z` for a timed
    event, `20380513` for an all-day one. Master ids are base32hex (no `_`), so
    a trailing segment that parses as a timestamp is unambiguously an instance.
    """
    _, sep, suffix = event_id.rpartition("_")
    if not sep or not suffix:
        return None
    fmt = "%Y%m%dT%H%M%SZ" if suffix.endswith("Z") else "%Y%m%d"
    try:
        return datetime.strptime(suffix, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _plan_legs_for_changed_events(
    session: Session,
    events: list[CalendarEvent],
    *,
    deleted_spans: list[tuple[datetime, datetime]] | None = None,
    account_key: str | None,
) -> None:
    """Derive commute legs around every changed physical event.

    This is the leg-creation path for brand-new events (which overlap
    nothing, so the reschedule path never fires for them). Recurring series
    arrive as bounded single instances — the sync window caps expansion at
    `WINDOW_DAYS`, and the daily re-baseline re-surfaces the instances
    sliding into view — so a new endless series only ever gets legs for the
    next week's occurrences, one pair per instance, never the whole series.
    Idempotent: unchanged legs diff to no-op patches.

    `deleted_spans` are the slots of removed events; a leg that was dodging
    one may sit up to the dodge cap away, so those spans are widened by the
    commute margin to catch both the stale leg and its relaxed placement."""
    if not get_settings().commute_enabled:
        return
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    spans: list[tuple[datetime, datetime]] = []
    span = _replan_span(events)
    if span is not None:
        spans.append(span)
    if deleted_spans:
        margin = commute_window_margin()
        spans.extend((start - margin, end + margin) for start, end in deleted_spans)
    if not spans:
        return

    await plan_commutes_window_best_effort(
        session,
        window_start=min(start for start, _ in spans),
        window_end=max(end for _, end in spans),
        account_key=account_key,
    )


def _replan_span(events: list[CalendarEvent]) -> tuple[datetime, datetime] | None:
    """Time range legs may need re-deriving around, or None.

    Any changed timed event counts: located ones are routing anchors, and
    online / location-less ones are avoid spans legs must dodge — a new
    meeting dropped onto a leg has to push it around. Only the planner's
    own commute legs and all-day events don't shape legs."""
    spans = [
        (ev.start, ev.end)
        for ev in events
        if not ev.all_day and not is_commute_event(ev)
    ]
    if not spans:
        return None
    return min(s for s, _ in spans), max(e for _, e in spans)


async def _sync_calendar(
    client,
    cid: str,
    *,
    token: str | None,
    baseline_iso: str | None,
    now: datetime,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[dict], str | None, datetime]:
    """Incremental sync when a fresh token exists, else a full baseline sync.

    Returns `(raw_items, new_token, baseline)` where `baseline` is the time the
    returned token's window was established (unchanged on an incremental pull).
    """
    if token and not _baseline_stale(baseline_iso, now):
        try:
            items, new_token = await client.sync_events(cid, sync_token=token)
            return items, new_token, datetime.fromisoformat(baseline_iso)
        except CalendarSyncTokenExpired:
            log.info("discover · sync token expired for %s; re-baselining", cid)

    log.info("discover · full baseline sync id=%s window=%s..%s", cid,
             window_start.isoformat(), window_end.isoformat())
    items, new_token = await client.sync_events(
        cid, time_min=window_start, time_max=window_end,
    )
    return items, new_token, now


def _baseline_stale(baseline_iso: str | None, now: datetime) -> bool:
    if not baseline_iso:
        return True
    try:
        base = datetime.fromisoformat(baseline_iso)
    except ValueError:
        return True
    return (now - base) > REBASELINE_INTERVAL


def _sync_task_schedule_from_event(session: Session, event: CalendarEvent) -> uuid.UUID | None:
    """Sync an externally-edited task event back into its task row.

    Pulls start (→ scheduled_date), duration (→ estimation), title, location
    and description. Only writes the DB — never patches the calendar back, so
    it can't ping-pong with the planner. Each field is compared against the
    value we'd have pushed, so unedited fields (aliased locations, the
    description+link merge) don't get clobbered on a round trip.
    """
    row = _task_for_event(session, event)
    if row is None:
        return None

    changed = False
    if row.calendar_event_id != event.id:
        row.calendar_event_id = event.id
        changed = True
    if not event.all_day and row.scheduled_date != event.start:
        row.scheduled_date = event.start
        changed = True
    for field, value in _synced_field_updates(event, row).items():
        setattr(row, field, value)
        changed = True

    if not changed:
        return None
    session.flush()
    return row.id


def _synced_field_updates(event: CalendarEvent, task: Task) -> dict:
    """Task-row fields that differ from what we last pushed to the event."""
    updates: dict = {}

    raw_summary = event.raw.get("summary")
    if raw_summary is not None and raw_summary != task.title:
        updates["title"] = raw_summary

    if event.description != _task_description(task):
        updates["description"] = _description_for_task(event, task)

    if event.location != resolve_location_alias(task.location):
        updates["location"] = event.location

    if not event.all_day:
        expected = _duration_minutes(task, get_settings())
        actual = round((event.end - event.start).total_seconds() / 60)
        if actual >= 1 and actual != expected:
            updates["estimation"] = actual

    return updates


def _description_for_task(event: CalendarEvent, task: Task) -> str | None:
    """Recover the task description from an edited event body.

    The event body is `description + "\\n\\n" + link`; strip a trailing,
    unedited link so it doesn't leak into `task.description` (and duplicate on
    the next push). If the link was itself edited away, keep the whole body.
    """
    desc = event.description
    if desc and task.link and desc.endswith(task.link):
        desc = desc[: -len(task.link)].rstrip("\n") or None
    return desc


async def _reschedule_deleted_task_event(
    session: Session,
    event_id: str,
    *,
    account_key: str | None,
) -> uuid.UUID | None:
    """Re-plan the task behind a deleted calendar event, blocking its old slot."""
    row = session.query(Task).filter(Task.calendar_event_id == event_id).one_or_none()
    if row is None:
        return None

    block = scheduled_interval_for(row)
    # Drop the dead event id so the planner creates a fresh mirror rather than
    # patching (and 404ing on) the event the user just deleted.
    row.calendar_event_id = None
    session.flush()

    log.info("discover · task event=%s deleted; rescheduling task=%s blocking old slot",
             event_id, row.id)
    result = await schedule_task(session, row, block=block, account_key=account_key)
    return row.id if result is not None else None


def _managed_conflicts(
    event: CalendarEvent,
    others: Iterable[CalendarEvent],
    gap: timedelta,
) -> list[CalendarEvent]:
    """Every managed write event overlapping `event` or sitting closer than
    `gap` to it — a task dragged to within the event buffer of a managed
    slot must move even without a literal overlap.

    All-day events only reach this marked busy (`_active_events` drops
    show-as-free ones) and block their whole days.
    """
    start, end = _effective_span(event)
    return [
        other for other in others
        if other.id != event.id
        and not other.all_day
        and is_managed_event(other)
        and start - gap < other.end
        and other.start < end + gap
    ]


def _effective_span(event: CalendarEvent) -> tuple[datetime, datetime]:
    if not event.all_day:
        return event.start, event.end
    from app.timezones import user_tz

    tz = user_tz()
    return (
        datetime.combine(event.start.date(), dt_time.min, tzinfo=tz),
        datetime.combine(event.end.date(), dt_time.min, tzinfo=tz),
    )


def _task_for_event(session: Session, event: CalendarEvent) -> Task | None:
    task_id = event.private_properties.get("task_id")
    if task_id:
        try:
            row = session.get(Task, uuid.UUID(task_id))
        except ValueError:
            row = None
        if row is not None:
            return row
    return session.query(Task).filter(Task.calendar_event_id == event.id).one_or_none()


def _active_events(items: Iterable[dict], calendar_id: str) -> list[CalendarEvent]:
    out: list[CalendarEvent] = []
    for item in items:
        if item.get("status") == "cancelled":
            continue
        if item.get("transparency") == "transparent":
            continue
        out.append(normalize(item, calendar_id))
    return out


def _calendar_ids(calendar_ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for cid in calendar_ids:
        clean = (cid or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _empty_summary() -> dict:
    return {
        "checked": 0,
        "updated": 0,
        "overlapping": 0,
        "scheduled_updates": 0,
        "deleted": 0,
    }
