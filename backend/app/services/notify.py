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


async def notify_task_created(task, *, start: datetime, end: datetime) -> None:
    """A new task was extracted and given a calendar slot."""
    parts = [f"Scheduled {_fmt_range(start, end)}"]
    if task.due_date:
        parts.append(f"Due {_fmt_when(to_user_tz(task.due_date))}")
    await notify(
        title=f"{_short_title(task)}",
        message="\n".join(parts),
        url=task_url(task.id),
        tag=task_tag(task.id),
        actions=_task_actions(),
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
