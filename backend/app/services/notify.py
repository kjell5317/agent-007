"""Push notifications via Home Assistant's REST API.

Every task notification carries `tag="task-<id>"` so the HA companion app
replaces stale notifications with fresh ones (one live notification per
task). Tapping a task notification opens the task detail modal in the
frontend.

Fire-and-forget: a failure here MUST NOT propagate to the caller —
otherwise a flaky HA install would turn into a flood of pipeline errors
that themselves want to fire notifications. We log and swallow.

HA is disabled when either `HOME_ASSISTANT_URL` or `HOME_ASSISTANT_TOKEN`
is empty — useful for local dev and CI where there's nothing to notify.
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx

from app.config import get_settings
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

_TIMEOUT = 5.0

# Action identifiers we send to HA. The companion app echoes these back in
# the `mobile_app_notification_action` event; our webhook reads them.
ACTION_CLOSE_TASK = "CLOSE_TASK"
ACTION_DISMISS_TASK = "DISMISS_TASK"
ACTION_RESCHEDULE_TASK = "RESCHEDULE_TASK"

# kotx run actions. kotx no longer sends its own Home Assistant approval/merge
# prompts (see docs/Home Assistant Notification Handoff) — we drive them from
# the transition webhook and dispatch these back to the kotx API in the
# notifications router. They map to the old kotx HA action ids: KOTX_START ↔
# `kotx-task-<id>-start`, KOTX_APPROVE/KOTX_MERGE ↔ `kotx-task-<id>-approve`.
ACTION_KOTX_START = "KOTX_START"
ACTION_KOTX_APPROVE = "KOTX_APPROVE"
ACTION_KOTX_MERGE = "KOTX_MERGE"
ACTION_KOTX_COMMENT = "KOTX_COMMENT"


async def notify(
    title: str,
    message: str,
    *,
    url: str | None = None,
    tag: str | None = None,
    actions: list[dict[str, str]] | None = None,
    sticky: bool = False,
    importance: str | None = None,
    channel: str | None = None,
    color: str | None = None,
    persistent: bool = False,
) -> None:
    """Send a single notification through HA's notify.<service>.

    `tag` lets the companion app replace stale notifications.
    `url` becomes `data.clickAction` (Android opens it on tap).
    `actions` becomes `data.actions` — buttons on the notification.
    `sticky=True` keeps the notification visible after the user taps the
    body or an action — without it the companion app dismisses on tap.
    `importance` (`high`/`max`) + `channel` escalate to a heads-up alert;
    `color` tints it. `persistent=True` makes it non-dismissable (Android,
    requires a `tag`) — clear it programmatically via `clear_task_notification`.
    """
    s = get_settings()
    if not s.home_assistant_url or not s.home_assistant_token:
        return

    endpoint = (
        s.home_assistant_url.rstrip("/") + "/api/services/notify/" + s.home_assistant_notify_service
    )
    data: dict[str, Any] = {}
    if url:
        data["clickAction"] = url
    if tag:
        data["tag"] = tag
    if actions:
        data["actions"] = actions
    if sticky:
        data["sticky"] = "true"
    if importance:
        data["importance"] = importance
    if channel:
        data["channel"] = channel
    if color:
        data["color"] = color
    if persistent:
        # Undismissable — must be paired with a tag so the app can key it, and
        # sticky so an action tap doesn't clear it out from under us.
        data["persistent"] = "true"
        data["sticky"] = "true"
    payload: dict[str, Any] = {"title": title, "message": message}
    if data:
        payload["data"] = data
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {s.home_assistant_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
        log.info("notify · sent title=%r tag=%r", title, tag)
    except Exception as exc:  # noqa: BLE001 — never let notify break the caller
        log.warning("home assistant notify failed: %s", exc)


def task_tag(task_id: UUID | str) -> str:
    return f"task-{task_id}"


def points_tag() -> str:
    return "points"


def task_url(task_id: UUID | str) -> str:
    """Frontend deep link for opening a task detail modal."""
    base = get_settings().task_default_url
    parts = urlsplit(base)
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, f"task/{task_id}"))


def _task_actions() -> list[dict[str, str]]:
    return [
        {"action": ACTION_CLOSE_TASK, "title": "Done"},
        {"action": ACTION_DISMISS_TASK, "title": "Dismiss"},
        {"action": ACTION_RESCHEDULE_TASK, "title": "Reschedule"},
    ]


def _warning_actions() -> list[dict[str, str]]:
    return [
        {"action": ACTION_CLOSE_TASK, "title": "Done"},
        {"action": ACTION_DISMISS_TASK, "title": "Dismiss"},
    ]


async def notify_task_created(
    task, *, start: datetime, end: datetime, primary_action: dict[str, str] | None = None
) -> None:
    """A new task was extracted and given a calendar slot.

    `primary_action` swaps the leading "Done" button for a task-specific action
    (kotx tasks put "Start"/"Comment"/… here) while keeping Dismiss + Reschedule
    — so a kotx task's first notification is the normal scheduled one with only
    that button changed."""
    parts = [f"Scheduled {_fmt_range(start, end)}"]
    if task.due_date:
        parts.append(f"Due {_fmt_when(to_user_tz(task.due_date))}")
    actions = _task_actions()
    if primary_action is not None:
        actions = [primary_action, *actions[1:]]
    await notify(
        title=f"{_short_title(task)}",
        message="\n".join(parts),
        url=task_url(task.id),
        tag=task_tag(task.id),
        actions=actions,
        sticky=True,
    )


_DISMISS_BUTTON = {"action": ACTION_DISMISS_TASK, "title": "Dismiss"}


async def notify_kotx_start(task, *, subject: str) -> None:
    """A kotx implement run drafted TASK.md and is waiting to start the write
    phase, on a task that was already surfaced (adoption / re-draft). The first
    notification carries this action inline via `notify_task_created`; this is
    the standalone fallback for the already-scheduled case."""
    await notify(
        title=f"Ready to start · {subject[:80]}",
        message="kotx drafted the brief — review it, then start the implementation.",
        url=task_url(task.id),
        tag=task_tag(task.id),
        actions=[{"action": ACTION_KOTX_START, "title": "Start"}, _DISMISS_BUTTON],
        sticky=True,
    )


async def notify_kotx_open_pr(task, *, subject: str) -> None:
    """A kotx implement run finished coding and proposed a pull request.
    Replaces kotx's removed "Open PR" prompt."""
    await notify(
        title=f"Open PR · {subject[:80]}",
        message="kotx finished coding and proposed a pull request — review and open it.",
        url=task_url(task.id),
        tag=task_tag(task.id),
        actions=[{"action": ACTION_KOTX_APPROVE, "title": "Open PR"}, _DISMISS_BUTTON],
        sticky=True,
    )


async def notify_kotx_confirm_merge(
    task, *, subject: str, approved_by: str | None = None, comment: str | None = None
) -> None:
    """An approving PR review moved a tracked implement run back to
    awaiting_approval with a merge proposal. Replaces kotx's removed
    "Confirm merge" prompt — names the approver and shows their (truncated)
    approval comment. Merge is the only offered action."""
    lines: list[str] = []
    if approved_by:
        lines.append(f"Approved by {approved_by}")
    if comment and comment.strip():
        lines.append(_clip(comment, 200))
    if not lines:
        lines.append("An approving review created a merge proposal.")
    await notify(
        title=f"Confirm merge · {subject[:80]}",
        message="\n".join(lines),
        url=task_url(task.id),
        tag=task_tag(task.id),
        actions=[{"action": ACTION_KOTX_MERGE, "title": "Merge"}],
        sticky=True,
    )


async def notify_kotx_review_ready(task, *, subject: str) -> None:
    """A kotx review run produced REVIEW.md and is waiting on a human decision,
    on a task that was already surfaced. The first notification carries the
    Comment action inline via `notify_task_created`; this is the standalone
    fallback for the already-scheduled case."""
    await notify(
        title=f"Review ready · {subject[:80]}",
        message="kotx finished a review — comment it, or open the task to approve.",
        url=task_url(task.id),
        tag=task_tag(task.id),
        actions=[{"action": ACTION_KOTX_COMMENT, "title": "Comment"}, _DISMISS_BUTTON],
        sticky=True,
    )


async def notify_no_slot(task) -> None:
    """No slot before the deadline after normal and extended windows fail.

    Escalated and undismissable: the user must resolve it (Done/Dismiss) or it
    clears itself once the task lands a slot (see `clear_task_notification`).
    """
    if task.due_date is None:
        return
    due = to_user_tz(task.due_date)
    await notify(
        title=f"⚠️ Could not schedule: {_short_title(task)}",
        message=f"No slot before due {_fmt_when(due)}",
        url=task_url(task.id),
        tag=task_tag(task.id),
        actions=_warning_actions(),
        importance="high",
        channel="Scheduling warnings",
        color="#f59e0b",
        persistent=True,
    )


async def notify_unroutable_location(task, location: str) -> None:
    """The task has a physical-looking location Google Maps can't route to —
    30-minute placeholder commutes were reserved instead of real ones. Uses
    its own tag so the regular "Scheduled" notification doesn't overwrite
    it."""
    await notify(
        title=f"⚠️ Can't route: {_short_title(task)}",
        message=(
            f"Google Maps found no route to \"{location[:120]}\".\n"
            "Reserved 30 min per leg as a placeholder — check the location."
        ),
        url=task_url(task.id),
        tag=f"route-{task.id}",
        channel="Scheduling warnings",
        color="#f59e0b",
    )


async def notify_unroutable_leg(
    *,
    origin: str,
    destination: str,
    depart: datetime,
    tag: str,
) -> None:
    """A calendar-derived commute leg has no route — a 30-minute ⚠️
    placeholder event was written instead. Counterpart of
    `notify_unroutable_location` for legs whose anchors are plain calendar
    events rather than tasks."""
    await notify(
        title="⚠️ Commute has no route",
        message=(
            f"{origin[:100]} → {destination[:100]}\n"
            f"Departs {_fmt_when(depart)}. Reserved 30 min per leg — check the event address."
        ),
        url=get_settings().task_default_url,
        tag=tag,
        channel="Scheduling warnings",
        color="#f59e0b",
    )


async def notify_points_penalty(task, *, points: int, reason: str) -> None:
    await notify(
        title="Points subtracted",
        message=f"-{points} points: {reason}\n{_short_title(task)}",
        url=task_url(task.id),
        tag=points_tag(),
        channel="Points",
        color="#dc2626",
    )


async def clear_task_notification(task_id: UUID | str) -> None:
    """Tell the HA companion app to remove the lingering notification for a
    task. Sent via the magic `message="clear_notification"` payload."""
    await notify(
        title="",
        message="clear_notification",
        tag=task_tag(task_id),
    )


async def notify_error(title: str, exc: BaseException, *, context: str | None = None) -> None:
    """Pipeline error. Not tied to a task — uses the dashboard fallback URL
    so a tap still lands somewhere useful."""
    tb_tail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    body_lines = [tb_tail]
    if context:
        body_lines.append(context)
    await notify(
        title=title,
        message="\n".join(body_lines)[:512],
        url=get_settings().task_default_url,
    )


def _short_title(task) -> str:
    return (task.title or "")[:90]


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt_when(dt: datetime) -> str:
    local = to_user_tz(dt)
    now = datetime.now(local.tzinfo)
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if local.year == now.year:
        return local.strftime("%b %d, %H:%M")
    return local.strftime("%b %d %Y, %H:%M")


def _fmt_range(start: datetime, end: datetime) -> str:
    s = to_user_tz(start)
    e = to_user_tz(end)
    if s.date() == e.date():
        return f"{_fmt_when(s)}–{e.strftime('%H:%M')}"
    return f"{_fmt_when(s)} → {_fmt_when(e)}"
