"""Read what surrounds a slot from the calendar and the task table, and turn
it into the feature snapshot stored with a schedule-log row."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import labels as labels_store
from app.db.clients import tasks as tasks_store
from app.db.models.task import Task
from app.services.plan.timeline import Item, TaskAttrs, slot_features
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

# Neighbours and day load never reach further than this around a slot.
CONTEXT_MARGIN = timedelta(days=1)


def busy_calendar_ids() -> list[str]:
    settings = get_settings()
    seen: set[str] = set()
    out: list[str] = []
    for cid in [settings.google_calendar_id, *settings.google_busy_calendar_ids]:
        clean = (cid or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


async def calendar_items(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    account_key: str | None = None,
    blockers: bool = False,
) -> list[Item]:
    """Fixed calendar events in `[start, end)`: busy calendars minus our own
    task mirrors (tasks come from the DB), commute legs (derived output in
    phase 1), show-as-free and declined events. A busy all-day event blocks
    its whole local days. `blockers` keeps show-as-free events at a physical
    place as `kind="blocker"` items — they shape routes, not occupancy."""
    from app.services.calendar import (
        is_commute_event,
        is_declined_event,
        is_free_event,
        is_location_blocker,
        is_task_event,
        list_events_between,
    )

    from app.services.calendar.attributes import event_attributes
    from app.services.location import is_online_location, resolve_location_alias

    ids = busy_calendar_ids()
    if not ids:
        return []
    events = await list_events_between(
        session, calendar_ids=ids, time_min=start, time_max=end, account_key=account_key,
    )
    names = labels_store.names_by_google_id(session)
    kept = []
    seen: set[str] = set()
    for ev in events:
        if ev.id in seen:
            continue
        seen.add(ev.id)
        if is_free_event(ev) and not (blockers and is_location_blocker(ev)):
            continue
        if is_task_event(ev) or is_commute_event(ev) or is_declined_event(ev):
            continue
        kept.append(ev)
    attributes = event_attributes(session, [(ev.calendar_id, ev.id) for ev in kept])
    tz = user_tz()
    out: list[Item] = []
    for ev in kept:
        label = names.get(ev.raw.get("eventLabelId") or "")
        work_type, effort = attributes.get((ev.calendar_id, ev.id), (None, None))
        if ev.all_day:
            lo = datetime.combine(ev.start.date(), time.min, tzinfo=tz)
            hi = datetime.combine(ev.end.date(), time.min, tzinfo=tz)
            out.append(Item(lo, hi, "event", label=label, event_id=ev.id, created=_created(ev)))
            continue
        location = None if is_online_location(ev.location) else resolve_location_alias(ev.location)
        out.append(Item(
            to_user_tz(ev.start), to_user_tz(ev.end),
            "blocker" if is_free_event(ev) else "event", label=label,
            work_type=work_type, effort=effort, event_id=ev.id, location=location,
            created=_created(ev),
        ))
    return out


def _created(event) -> datetime | None:
    raw = event.raw.get("created")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def task_attrs(task: Task, label_defaults: dict[str, tuple[str | None, str | None]]) -> TaskAttrs:
    """Scheduling attributes with the label's defaults filling gaps."""
    default_wt, default_eff = label_defaults.get(task.label or "", (None, None))
    return TaskAttrs(
        task_id=task.id,
        due=to_user_tz(task.due_date),
        deadline_kind=task.deadline_kind or "firm",
        label=task.label,
        work_type=task.work_type or default_wt,
        effort=task.effort or default_eff,
    )


def task_item(task: Task, attrs: TaskAttrs, start: datetime, end: datetime) -> Item:
    from app.services.location import is_online_location, resolve_location_alias

    location = None if is_online_location(task.location) else resolve_location_alias(task.location)
    return Item(
        to_user_tz(start), to_user_tz(end), "task", label=attrs.label,
        work_type=attrs.work_type, effort=attrs.effort, task_id=task.id, location=location,
        event_id=getattr(task, "calendar_event_id", None),
    )


def db_task_items(
    session: Session,
    start: datetime,
    end: datetime,
    label_defaults: dict[str, tuple[str | None, str | None]],
) -> list[Item]:
    """Open and closed tasks whose booked slot lies around `[start, end)` —
    closed ones still occupied their time when the slot ran."""
    from app.services.plan.schedule import _duration_minutes

    settings = get_settings()
    rows = session.scalars(
        select(Task).where(
            Task.scheduled_date >= start - timedelta(days=1),
            Task.scheduled_date < end,
            Task.is_container.is_(False),
        )
    ).all()
    statuses = tasks_store.latest_status_for(session, [row.id for row in rows])
    out: list[Item] = []
    for row in rows:
        if statuses.get(row.id, "open") not in {"open", "closed"}:
            continue
        slot_start = to_user_tz(row.scheduled_date)
        slot_end = slot_start + timedelta(minutes=_duration_minutes(row, settings))
        if slot_end <= start:
            continue
        out.append(task_item(row, task_attrs(row, label_defaults), slot_start, slot_end))
    return out


async def features_snapshot(
    session: Session,
    task: Task,
    start: datetime,
    end: datetime,
    *,
    now: datetime | None = None,
    prev: tuple[datetime, datetime] | None = None,
) -> dict | None:
    """`{"slot": φ, "prev": φ, "meta": …}` for a log row. Best effort: a
    calendar failure logs and returns None rather than blocking the action
    being logged."""
    now = now or datetime.now(user_tz())
    spans = [(start, end)] + ([prev] if prev else [])
    lo = min(s for s, _ in spans) - CONTEXT_MARGIN
    hi = max(e for _, e in spans) + CONTEXT_MARGIN
    try:
        label_defaults = labels_store.defaults_by_name(session)
        items = await calendar_items(session, lo, hi)
        items += db_task_items(session, lo, hi, label_defaults)
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot · context unavailable task=%s err=%s", task.id, exc)
        return None
    attrs = task_attrs(task, label_defaults)
    kw = {"now": now, "items": items, "tz": user_tz()}
    out: dict = {
        "slot": slot_features(attrs, start, end, **kw),
        "meta": meta(task, attrs),
    }
    if prev is not None:
        out["prev"] = slot_features(attrs, prev[0], prev[1], **kw)
    return out


def meta(task: Task, attrs: TaskAttrs) -> dict:
    from app.services.plan.schedule import estimate_minutes

    return {
        # The duration model learns from the agent's raw estimate: training on
        # the corrected one would learn the correction away.
        "estimate": task.agent_estimation or estimate_minutes(task, get_settings()),
        "label": attrs.label,
        "work_type": attrs.work_type,
        "effort": attrs.effort,
    }
