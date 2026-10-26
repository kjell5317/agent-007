"""Writes the solved commute legs to the calendar.

The legs come out of the solve (`app.services.plan.routing`); this module
diffs them against the managed commute events on the write calendar. Legs
are identified by `(origin_anchor, dest_anchor)`, so a moved anchor's legs
are patched in place rather than deleted and recreated. It also moves the
pre-event reminder onto the arriving leg, alerts on unroutable or
conflicting legs, and sweeps legs outside the live window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.calendar import (
    SILENT_REMINDERS,
    CalendarEvent,
    commute_leg_key,
    commute_private_properties,
    create_event,
    delete_event,
    is_commute_event,
    is_task_event,
    list_events_between,
    patch_event,
    popup_reminders,
    private_properties,
    reminders_differ,
)
from app.services.commute.legs import FAILED_MODE, HOME, PlannedLeg
from app.services.commute.flip_link import flip_url
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

# Marks a foreign (user-created) event whose reminders we silenced because an
# arriving leg carries the notification — so we know to restore the calendar
# default when the leg goes away, and never touch events the user silenced
# themselves.
_REMINDERS_MANAGED_PROP = "reminders_managed"


def _reminders_for_leg(leg: PlannedLeg) -> dict:
    """Legs arriving at an anchor carry the pre-event popup; rides home stay
    silent (explicitly, so calendar defaults don't fire on them)."""
    if leg.dest_anchor == HOME:
        return SILENT_REMINDERS
    return popup_reminders(get_settings().reminder_lead_minutes)


def _desired_anchor_reminders(
    ev: CalendarEvent,
    has_arriving_leg: bool,
    settings,
) -> tuple[dict | None, dict | None]:
    """`(reminders, private_property_patch)` an anchor should get, or
    `(None, None)` to leave it alone.

    Task events are fully managed: silent behind an arriving leg, popup
    otherwise. Foreign events are only silenced when a leg takes over —
    marked so the calendar default comes back when the leg disappears —
    and never touched if the user silenced them independently."""
    if is_task_event(ev):
        desired = (
            SILENT_REMINDERS
            if has_arriving_leg
            else popup_reminders(settings.reminder_lead_minutes)
        )
        return desired, None

    managed = private_properties(ev).get(_REMINDERS_MANAGED_PROP) == "true"
    if has_arriving_leg:
        if managed:
            return SILENT_REMINDERS, None
        return SILENT_REMINDERS, {_REMINDERS_MANAGED_PROP: "true"}
    if managed:
        return {"useDefault": True}, {_REMINDERS_MANAGED_PROP: ""}
    return None, None


async def _sync_anchor_reminders(
    session: Session,
    *,
    events: list[CalendarEvent],
    legs: list[PlannedLeg],
    window: tuple[datetime, datetime],
    calendar_id: str,
    settings,
    account_key: str | None,
) -> None:
    """Move the pre-start popup onto the arriving leg (and back) for every
    anchor on the write calendar inside the replanned window."""
    arriving = {leg.dest_anchor for leg in legs if leg.dest_anchor != HOME}
    for ev in events:
        if ev.calendar_id != calendar_id or ev.all_day or is_commute_event(ev):
            continue
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        desired, prop_patch = _desired_anchor_reminders(ev, ev.id in arriving, settings)
        if desired is None:
            continue
        differ = reminders_differ(ev, desired)
        claiming = prop_patch is not None and prop_patch.get(_REMINDERS_MANAGED_PROP) == "true"
        if claiming and not differ:
            # Already silent by the user's own hand — don't claim it, or the
            # leg's later removal would "restore" defaults they never wanted.
            continue
        if not differ and prop_patch is None:
            continue
        try:
            await patch_event(
                session,
                calendar_id=calendar_id,
                event_id=ev.id,
                reminders=desired,
                private_properties=prop_patch,
                account_key=account_key,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · reminder sync failed event=%s err=%s", ev.id, exc)


# How far back the past-leg sweep looks. Wide enough to also catch strays
# that pre-window-clamp replans wrote next to years-old events.
_PAST_LEG_LOOKBACK = timedelta(days=1825)
# How far beyond the lookahead horizon the stray sweep looks — far-future
# legs come from task placements at `due - lead`, so a year covers any
# realistic due date.
_STRAY_LEG_LOOKAHEAD = timedelta(days=365)


async def delete_stray_commute_legs(
    session: Session,
    *,
    account_key: str | None = None,
) -> int:
    """Delete managed commute events outside `[now, now + lookahead]` — a
    ride in the past is dead weight once it's folded into the stored bike /
    user position (`app.services.commute.state`), and legs written beyond
    the horizon (strays from before the horizon cap) get re-derived once
    their anchor slides into the window."""
    settings = get_settings()
    write_calendar_id = (settings.google_calendar_id or "").strip()
    if not write_calendar_id:
        return 0
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=settings.commute_lookahead_days)
    deleted = 0
    for time_min, time_max in (
        (now - _PAST_LEG_LOOKBACK, now),
        (horizon, horizon + _STRAY_LEG_LOOKAHEAD),
    ):
        events = await list_events_between(
            session,
            calendar_ids=[write_calendar_id],
            time_min=time_min,
            time_max=time_max,
            account_key=account_key,
        )
        finished = [ev for ev in events if is_commute_event(ev) and not ev.all_day and ev.end <= now]
        if finished:
            from app.services.commute.state import fold_finished, leg_record

            fold_finished(session, [r for ev in finished if (r := leg_record(ev)) is not None])
            session.commit()
        for ev in events:
            if ev.all_day or not is_commute_event(ev):
                continue
            if ev.end > now and ev.start < horizon:
                # Running or boundary-straddling leg — still live.
                continue
            try:
                await delete_event(
                    session,
                    calendar_id=write_calendar_id,
                    event_id=ev.id,
                    account_key=account_key,
                )
                deleted += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("commute · stray leg delete failed event=%s err=%s", ev.id, exc)
    return deleted


async def _notify_new_failures(legs: list[PlannedLeg]) -> None:
    from app.services.notify import notify_unroutable_leg

    for leg in legs:
        await notify_unroutable_leg(
            origin=leg.origin,
            destination=leg.destination,
            depart=leg.depart,
            tag=f"route-{leg.origin_anchor}-{leg.dest_anchor}",
        )


def _immovable_conflicts(
    legs: list[PlannedLeg],
    obstacles: list[tuple[str, str, datetime, datetime]],
) -> dict[tuple[str, str], tuple[str, str]]:
    """Legs still colliding with an `(id, label, start, end)` obstacle after
    task re-placement — located anchors and online/location-less events the
    dodge couldn't clear. Only immovable things remain in their way."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for leg in legs:
        for oid, label, start, end in obstacles:
            if oid in leg.key:
                continue
            if leg.depart < end and start < leg.arrive:
                out[leg.key] = (oid, label)
                break
    return out


def _conflict_tag(key: tuple[str, str]) -> str:
    return f"conflict-{key[0]}-{key[1]}"


async def _notify_new_conflicts(conflicts: list[tuple[PlannedLeg, str]]) -> None:
    from app.services.notify import notify_leg_conflict

    for leg, blocker_label in conflicts:
        await notify_leg_conflict(
            destination=leg.destination,
            blocker=blocker_label,
            depart=leg.depart,
            tag=_conflict_tag(leg.key),
        )


async def _write_legs(
    session: Session,
    legs: list[PlannedLeg],
    *,
    calendar_id: str,
    existing: list[CalendarEvent],
    window: tuple[datetime, datetime],
    account_key: str | None,
    conflicts: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[int, list[PlannedLeg], list[tuple[PlannedLeg, str]]]:
    conflicts = conflicts or {}
    desired = {leg.key: leg for leg in legs}
    existing_by_key: dict[tuple[str, str], CalendarEvent] = {}
    legacy: list[CalendarEvent] = []
    duplicates: list[CalendarEvent] = []
    for ev in existing:
        key = commute_leg_key(ev)
        if key is None:
            legacy.append(ev)
        elif key in existing_by_key:
            # Same leg identity written twice (a diff that couldn't see the
            # first copy) — redundant by construction, delete on sight.
            duplicates.append(ev)
        else:
            existing_by_key[key] = ev

    for ev in duplicates:
        log.warning(
            "commute · duplicate leg %s (%s) — deleting",
            commute_leg_key(ev),
            ev.id,
        )
        try:
            await delete_event(
                session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · duplicate delete failed event=%s err=%s", ev.id, exc)

    for ev in legacy:
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        try:
            await delete_event(
                session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · legacy delete failed event=%s err=%s", ev.id, exc)

    for key, ev in existing_by_key.items():
        if key in desired:
            continue
        if not _overlaps(ev.start, ev.end, window[0], window[1]):
            continue
        try:
            await delete_event(
                session, calendar_id=calendar_id, event_id=ev.id, account_key=account_key
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("commute · stale delete failed event=%s err=%s", ev.id, exc)

    written = 0
    new_failures: list[PlannedLeg] = []
    new_conflicts: list[tuple[PlannedLeg, str]] = []
    for key, leg in desired.items():
        summary = _summary_for(leg)
        description = _description_for(leg)
        blocker = conflicts.get(key)
        props = commute_private_properties(
            origin_anchor=leg.origin_anchor,
            dest_anchor=leg.dest_anchor,
            mode=leg.mode,
        )
        # Conflict marker: persisted on the leg so replans that re-derive the
        # same collision don't re-alert; cleared (empty) when it resolves.
        props["conflict"] = blocker[0] if blocker is not None else ""
        reminders = _reminders_for_leg(leg)
        current = existing_by_key.get(key)
        was_conflicting = bool(private_properties(current).get("conflict")) if current else False
        # Notify only when a leg *becomes* failed/conflicting — a replan that
        # re-writes the same state shouldn't re-alert on every pass.
        newly_failed = leg.mode == FAILED_MODE and (
            current is None or _commute_mode(current) != FAILED_MODE
        )
        newly_conflicting = blocker is not None and not was_conflicting
        try:
            if current is None:
                await create_event(
                    session,
                    calendar_id=calendar_id,
                    summary=summary,
                    start=leg.depart,
                    end=leg.arrive,
                    description=description,
                    location=leg.destination,
                    private_properties=props,
                    reminders=reminders,
                    account_key=account_key,
                )
            elif _needs_patch(current, leg, summary, description, reminders, props["conflict"]):
                await patch_event(
                    session,
                    calendar_id=calendar_id,
                    event_id=current.id,
                    summary=summary,
                    start=leg.depart,
                    end=leg.arrive,
                    description=description,
                    location=leg.destination,
                    private_properties=props,
                    reminders=reminders,
                    account_key=account_key,
                )
            written += 1
            if newly_failed:
                new_failures.append(leg)
            if newly_conflicting:
                new_conflicts.append((leg, blocker[1]))
            elif blocker is None and was_conflicting:
                from app.services.notify import clear_notification_tag

                await clear_notification_tag(_conflict_tag(key))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "commute · upsert failed origin=%s dest=%s err=%s",
                leg.origin_anchor,
                leg.dest_anchor,
                exc,
            )
    return written, new_failures, new_conflicts


def _needs_patch(
    ev: CalendarEvent,
    leg: PlannedLeg,
    summary: str,
    description: str,
    reminders: dict,
    conflict_marker: str = "",
) -> bool:
    # The Updated stamp changes on every derivation — comparing it would turn
    # every replan into a patch of every leg.
    return (
        ev.summary != summary
        or _without_updated_line(ev.description or "") != _without_updated_line(description)
        or (ev.location or "") != leg.destination
        or _epoch(ev.start) != _epoch(leg.depart)
        or _epoch(ev.end) != _epoch(leg.arrive)
        or reminders_differ(ev, reminders)
        or (private_properties(ev).get("conflict") or "") != conflict_marker
    )


def _without_updated_line(description: str) -> str:
    return "\n".join(line for line in description.splitlines() if not line.startswith("Updated:"))


def _epoch(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


def _summary_for(leg: PlannedLeg) -> str:
    # The destination already lives in the event's location field.
    if leg.mode == FAILED_MODE:
        return "⚠️ No route"
    return "🚲 Travel" if leg.mode == "bicycling" else "🚆 Travel"


def _description_for(leg: PlannedLeg) -> str:
    # Destination sits in the location field, the mode in the emoji title.
    # `leg_record` reads the origin back from the plain "From:" line; Google
    # renders the anchors.
    lines = [f"From: {leg.origin}"]
    if leg.note:
        lines.append(leg.note)
    if leg.mode == FAILED_MODE:
        lines.append("No route found — reserved 30 min. Check the address.")
    links = [_anchor(_navigation_url(leg), "Navigate")]
    switch = flip_url(leg.origin_anchor, leg.dest_anchor, leg.depart, leg.arrive, leg.mode)
    if switch:
        target = "bike" if leg.mode == "transit" else "transit"
        links.append(_anchor(switch, f"Switch to {target}"))
    lines.append(" · ".join(links))
    updated = to_user_tz(datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M")
    lines.append(f"Updated: {updated}")
    return "\n".join(lines)


def _anchor(url: str, text: str) -> str:
    return f'<a href="{escape(url)}">{text}</a>'


def _navigation_url(leg: PlannedLeg) -> str:
    """Google Maps universal deep link — opens the Maps app with the route
    pre-filled (the Distance Matrix API itself returns no links). Failed
    legs get no travelmode so Maps picks whatever it can find. No
    departure_time: the deep link ignores it."""
    params = {"api": "1", "origin": leg.origin, "destination": leg.destination}
    if leg.mode != FAILED_MODE:
        params["travelmode"] = leg.mode
    return f"https://www.google.com/maps/dir/?{urlencode(params)}"


def _commute_mode(ev: CalendarEvent) -> str | None:
    mode = ev.private_properties.get("mode")
    if mode:
        return mode
    # Legacy legs carried the mode as a description line.
    for line in (ev.description or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "mode":
            return value.strip().lower() or None
    return None


def _overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return start_a < end_b and start_b < end_a


