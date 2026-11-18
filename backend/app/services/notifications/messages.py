"""Action ids, tags and one pure builder per notification kind.

A task's notifications carry `tag="task-<id>-<kind>"`, one tag per kind, so
clearing one (a warning that resolved) never removes another (the new-task
push). Tapping a task notification opens the task in the frontend.
"""

from __future__ import annotations

import traceback
import uuid
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from app.config import get_settings
from app.services.notifications.push import Message
from app.timezones import to_user_tz

# Buttons on our pushes; the action router reads them back.
ACTION_DISMISS_TASK = "DISMISS_TASK"
ACTION_REOPEN_TASK = "REOPEN_TASK"
ACTION_START_TASK = "START_TASK"
ACTION_SNOOZE_START = "SNOOZE_START"
ACTION_NOT_TODAY = "NOT_TODAY"
ACTION_DRAFT_REGENERATE = "DRAFT_REGENERATE"
ACTION_DRAFT_DISCARD = "DRAFT_DISCARD"
# Sent by Home Assistant automations with a `task_id`, not by our buttons.
ACTION_CLOSE_TASK = "CLOSE_TASK"
ACTION_RESCHEDULE_TASK = "RESCHEDULE_TASK"
# Fired by the HA morning/evening routines — not tied to a task. DAY answers
# with how long the user has been awake (now − last night's sleep end); NIGHT
# with the minutes until 45 min before the next event. See the router.
ACTION_DAY = "DAY"
ACTION_NIGHT = "NIGHT"

_TAG_PREFIXES = ("task-", "handoff-")
_WARNING_CHANNEL = "Scheduling warnings"
_INFO_CHANNEL = "Scheduling info"
_WARNING_COLOR = "#f59e0b"


def task_tag(task_id: uuid.UUID | str, kind: str) -> str:
    return f"task-{task_id}-{kind}"


def handoff_tag(task_id: uuid.UUID | str) -> str:
    return f"handoff-{task_id}"


def task_id_from_tag(tag: str | None) -> uuid.UUID | None:
    for prefix in _TAG_PREFIXES:
        if tag and tag.startswith(prefix):
            try:
                return uuid.UUID(tag[len(prefix):len(prefix) + 36])
            except ValueError:
                return None
    return None


def task_url(task_id: uuid.UUID | str) -> str:
    """Frontend deep link for opening a task detail modal."""
    parts = urlsplit(get_settings().public_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, f"task/{task_id}"))


# --- Event notifications ------------------------------------------------------


def error(title: str, exc: BaseException, *, context: str | None = None) -> Message:
    lines = ["".join(traceback.format_exception_only(type(exc), exc)).strip()]
    if context:
        lines.append(context)
    return Message(title=title, body="\n".join(lines)[:512], url=get_settings().public_url)


def plain_error(title: str, body: str) -> Message:
    return Message(title=title, body=body[:512], url=get_settings().public_url)


def new_task(task) -> Message:
    """An agent created a task the user hasn't seen yet."""
    return Message(
        title=short_title(task),
        body=_due_line(task.due_date, task.deadline_kind),
        tag=task_tag(task.id, "created"),
        url=task_url(task.id),
        actions=((ACTION_DISMISS_TASK, "Dismiss"),),
    )


def done_for_you(task, *, headline: str, text: str | None, reopen: bool = True) -> Message:
    """An agent finished a task on its own: answered it, closed it from an
    input, or completed a hand-off. Reopen undoes a wrong close."""
    return Message(
        title=f"{headline}: {short_title(task)}",
        body=clip(text, 300) if text and text.strip() else headline,
        tag=task_tag(task.id, "done"),
        url=task_url(task.id),
        actions=((ACTION_REOPEN_TASK, "Reopen"),) if reopen else (),
    )


def draft_ready(task, *, mail_url: str, note: str | None = None) -> Message:
    """A reply draft sits in the mailbox; tapping opens the thread with it.
    `note` lists the open questions."""
    return Message(
        title=f"Draft ready: {short_title(task)}",
        body=clip(note, 300) if note else "Review and send it from your mailbox.",
        tag=handoff_tag(task.id),
        url=mail_url,
        actions=((ACTION_DRAFT_REGENERATE, "Regenerate"), (ACTION_DRAFT_DISCARD, "Discard")),
        sticky=True,
    )


def draft_stale(task, *, mail_url: str) -> Message:
    return Message(
        title=f"New reply in the thread: {short_title(task)}",
        body="Your draft doesn't cover it yet.",
        tag=handoff_tag(task.id),
        url=mail_url,
        actions=((ACTION_DRAFT_REGENERATE, "Regenerate"), (ACTION_DRAFT_DISCARD, "Discard")),
        sticky=True,
    )


def start_cue(task, *, start: datetime, end: datetime) -> Message:
    return Message(
        title=f"Time for {short_title(task)}",
        body=f"Planned {fmt_range(start, end)}",
        tag=task_tag(task.id, "cue"),
        url=task_url(task.id),
        actions=(
            (ACTION_START_TASK, "Start"),
            (ACTION_SNOOZE_START, "In 30 min"),
            (ACTION_NOT_TODAY, "Not today"),
        ),
        sticky=True,
    )


# --- State notifications --------------------------------------------------------

_DEADLINE_TITLES = {
    "late": "Planned after its deadline",
    "unplaced": "No slot before its deadline",
    "later": "Not planned before its deadline",
    "passed": "Deadline passed",
}


def past_deadline(
    *, task_id: uuid.UUID, title: str, due: datetime, deadline_kind: str, case: str,
    slot: tuple[datetime, datetime] | None,
) -> Message:
    if case == "late" and slot is not None:
        body = f"Due {fmt_when(due)} · planned {fmt_range(*slot)}"
    elif case == "passed":
        body = f"Was due {fmt_when(due)}"
    else:
        body = f"Due {fmt_when(due)}"
    hard = deadline_kind == "hard"
    return Message(
        title=f"{'⚠️ ' if hard else ''}{_DEADLINE_TITLES[case]}: {title[:90]}",
        body=body,
        tag=task_tag(task_id, "deadline"),
        url=task_url(task_id),
        importance="high" if hard else None,
        channel=_WARNING_CHANNEL if hard else _INFO_CHANNEL,
        color=_WARNING_COLOR if hard else None,
    )


def leg_conflict(*, tag: str, destination: str, blocker: str, depart: datetime) -> Message:
    """A commute leg had to be written over (or too close to) an event nothing
    can move — the user has to resolve the collision by hand."""
    return Message(
        title="⚠️ Commute conflicts with an event",
        body=(
            f"Leg to {destination[:100]} departing {fmt_when(depart)} "
            f"collides with {clip(blocker, 100)}. Nothing movable — resolve by hand."
        ),
        tag=tag,
        url=get_settings().public_url,
        channel=_WARNING_CHANNEL,
        color=_WARNING_COLOR,
    )


def no_route(*, tag: str, address: str, depart: datetime) -> Message:
    """No route to or from an event's address — a 30-minute ⚠️ placeholder
    leg was written instead."""
    return Message(
        title="⚠️ Commute has no route",
        body=(
            f"{address[:200]}\n"
            f"First leg departs {fmt_when(depart)}. Reserved 30 min per leg — check the event address."
        ),
        tag=tag,
        url=get_settings().public_url,
        channel=_WARNING_CHANNEL,
        color=_WARNING_COLOR,
    )


# --- Formatting -------------------------------------------------------------------


def short_title(task) -> str:
    return (task.title or "")[:90]


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fmt_when(dt: datetime) -> str:
    local = to_user_tz(dt)
    now = datetime.now(local.tzinfo)
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if local.year == now.year:
        return local.strftime("%b %d, %H:%M")
    return local.strftime("%b %d %Y, %H:%M")


def fmt_range(start: datetime, end: datetime) -> str:
    s = to_user_tz(start)
    e = to_user_tz(end)
    if s.date() == e.date():
        return f"{fmt_when(s)}–{e.strftime('%H:%M')}"
    return f"{fmt_when(s)} → {fmt_when(e)}"


def _due_line(due: datetime | None, deadline_kind: str | None) -> str:
    if due is None:
        return "No deadline"
    return f"Due {fmt_when(due)} · {deadline_kind or 'hard'} deadline"
