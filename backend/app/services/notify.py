"""Push notifications via Home Assistant's REST API.

Every task notification carries `tag="task-<id>"` so the HA companion app
replaces stale notifications with fresh ones (one live notification per
task). Tapping any task notification opens its `link` — or
`settings.task_default_url` when the task has none.

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
from uuid import UUID

import httpx

from app.config import get_settings
from app.timezones import to_user_tz

log = logging.getLogger(__name__)

_TIMEOUT = 5.0

# Action identifiers we send to HA. The companion app echoes these back in
# the `mobile_app_notification_action` event; our webhook reads them.
ACTION_EXTEND_WINDOW = "EXTEND_WINDOW"


async def notify(
    title: str,
    message: str,
    *,
    url: str | None = None,
    tag: str | None = None,
    actions: list[dict[str, str]] | None = None,
    sticky: bool = False,
) -> None:
    """Send a single notification through HA's notify.<service>.

    `tag` lets the companion app replace stale notifications.
    `url` becomes `data.clickAction` (Android opens it on tap).
    `actions` becomes `data.actions` — buttons on the notification.
    `sticky=True` keeps the notification visible after the user taps the
    body or an action — without it the companion app dismisses on tap.
    """
    s = get_settings()
    if not s.home_assistant_url or not s.home_assistant_token:
        return

    endpoint = (
        s.home_assistant_url.rstrip("/")
        + "/api/services/notify/"
        + s.home_assistant_notify_service
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


# Cap on URI action buttons we attach to a task notification. Android allows
# three actions total per notification; the EXTEND button can occupy one of
# them on no-slot notifications, so two URI slots leaves us room without
# clipping.
MAX_LINK_ACTIONS = 2


def _link_actions(task) -> list[dict[str, str]]:
    """URI buttons for opening task-related links in the phone browser.

    Currently we only surface `task.link`. The list is capped so callers
    can blindly extend it with other actions (EXTEND_WINDOW etc.) without
    blowing past Android's per-notification action limit.
    """
    actions: list[dict[str, str]] = []
    link = (task.link or "").strip()
    if link:
        actions.append({"action": "URI", "title": "Open link", "uri": link})
    return actions[:MAX_LINK_ACTIONS]


async def notify_task_scheduled(
    task,
    *,
    start: datetime,
    end: datetime,
    is_fresh: bool,
) -> None:
    """Task got a slot. `is_fresh=False` means it had a slot before and was moved."""
    kind = "Scheduled" if is_fresh else "Rescheduled"
    parts = [_fmt_range(start, end)]
    if task.estimation:
        parts.append(f"{task.estimation}min")
    await notify(
        title=f"{kind}: {_short_title(task)}",
        message=" · ".join(parts),
        url=get_settings().task_default_url,
        tag=task_tag(task.id),
        actions=_link_actions(task) or None,
        sticky=True,
    )


async def notify_no_slot(task, *, extended: bool = False) -> None:
    """No slot before the deadline. Includes an Extend-window action button
    on the first attempt; on a re-attempt with the extended window already
    applied, drops the button (nothing more to widen automatically)."""
    if task.due_date is None:
        return
    due = to_user_tz(task.due_date)
    title = "Could not schedule" + (" (extended)" if extended else "")
    actions: list[dict[str, str]] = []
    if not extended:
        actions.append({"action": ACTION_EXTEND_WINDOW, "title": "Try 20–24 & 8–10"})
    actions.extend(_link_actions(task))
    await notify(
        title=title,
        message=f"{_short_title(task)} · due {_fmt_when(due)}",
        url=get_settings().task_default_url,
        tag=task_tag(task.id),
        actions=actions or None,
        sticky=True,
    )


async def notify_batch_rescheduled(items: list[tuple[Any, datetime, datetime]]) -> None:
    """Single summary for many tasks moved at once (e.g. commute replan).

    Uses a fixed `batch-reschedule` tag so consecutive batches replace
    each other rather than piling up. Per-task notifications are still
    available via `notify_task_scheduled` for callers that prefer them.
    """
    if not items:
        return
    lines = []
    for task, start, _end in items[:5]:
        lines.append(f"• {_short_title(task)} → {_fmt_when(start)}")
    if len(items) > 5:
        lines.append(f"+ {len(items) - 5} more")
    await notify(
        title=f"Rescheduled {len(items)} tasks",
        message="\n".join(lines),
        url=get_settings().task_default_url,
        tag="batch-reschedule",
        sticky=True,
    )


async def clear_task_notification(task_id: UUID | str) -> None:
    """Tell the HA companion app to remove the lingering notification for a
    task. Sent via the magic `message="clear_notification"` payload."""
    await notify(
        title="",
        message="clear_notification",
        tag=task_tag(task_id),
    )


async def notify_agent_task_updated(task, *, changes: dict) -> None:
    fields = ", ".join(sorted(changes.keys())) or "task"
    await notify(
        title=f"Agent updated: {_short_title(task)}",
        message=f"Changed: {fields}",
        url=get_settings().task_default_url,
        tag=task_tag(task.id),
        actions=_link_actions(task) or None,
        sticky=True,
    )


async def notify_agent_task_closed(task) -> None:
    await notify(
        title=f"Agent closed: {_short_title(task)}",
        message="Marked complete by follow-up",
        url=get_settings().task_default_url,
        tag=task_tag(task.id),
        actions=_link_actions(task) or None,
        sticky=True,
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
