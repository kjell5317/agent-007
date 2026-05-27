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
) -> None:
    """Send a single notification through HA's notify.<service>.

    `tag` lets the companion app replace stale notifications.
    `url` becomes `data.clickAction` (Android opens it on tap).
    `actions` becomes `data.actions` — buttons on the notification.
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


def _task_url(task) -> str:
    link = (task.link or "").strip()
    return link or get_settings().task_default_url


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
        url=_task_url(task),
        tag=task_tag(task.id),
    )


async def notify_no_slot(task, *, extended: bool = False) -> None:
    """No slot before the deadline. Includes an Extend-window action button
    on the first attempt; on a re-attempt with the extended window already
    applied, drops the button (nothing more to widen automatically)."""
    if task.due_date is None:
        return
    due = task.due_date.astimezone()
    title = "Could not schedule" + (" (extended)" if extended else "")
    actions: list[dict[str, str]] | None = None
    if not extended:
        actions = [
            {"action": ACTION_EXTEND_WINDOW, "title": "Extend 08–24"},
        ]
    await notify(
        title=title,
        message=f"{_short_title(task)} · due {_fmt_when(due)}",
        url=_task_url(task),
        tag=task_tag(task.id),
        actions=actions,
    )


async def notify_agent_task_updated(task, *, changes: dict) -> None:
    fields = ", ".join(sorted(changes.keys())) or "task"
    await notify(
        title=f"Agent updated: {_short_title(task)}",
        message=f"Changed: {fields}",
        url=_task_url(task),
        tag=task_tag(task.id),
    )


async def notify_agent_task_closed(task) -> None:
    await notify(
        title=f"Agent closed: {_short_title(task)}",
        message="Marked complete by follow-up",
        url=_task_url(task),
        tag=task_tag(task.id),
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
    local = dt.astimezone()
    now = datetime.now(local.tzinfo)
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if local.year == now.year:
        return local.strftime("%b %d, %H:%M")
    return local.strftime("%b %d %Y, %H:%M")


def _fmt_range(start: datetime, end: datetime) -> str:
    s = start.astimezone()
    e = end.astimezone()
    if s.date() == e.date():
        return f"{_fmt_when(s)}–{e.strftime('%H:%M')}"
    return f"{_fmt_when(s)} → {_fmt_when(e)}"
