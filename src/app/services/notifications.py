"""Push notifications via Home Assistant's REST API.

Two call shapes:

  * `notify_task_created(task, raw)` — fired by the agent runner on task creation.
  * `notify_error(title, exc, *, context)` — fired wherever the pipeline catches
    an exception we want surfaced to the phone.

Both are fire-and-forget: a failure here MUST NOT propagate to the caller —
otherwise a flaky HA install would turn into a flood of pipeline errors that
themselves want to fire notifications. We log and swallow.

HA is disabled when either `HOME_ASSISTANT_URL` or `HOME_ASSISTANT_TOKEN` is
empty — useful for local dev and CI where there's nothing to notify.
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_TIMEOUT = 5.0


async def notify(title: str, message: str, url: str | None = None) -> None:
    """Send a single notification through HA's notify.<service>.

    When `url` is set, attach it as `data.clickAction` so tapping the phone
    notification opens it (handled by the HA companion app).
    """
    s = get_settings()
    if not s.home_assistant_url or not s.home_assistant_token:
        return

    endpoint = (
        s.home_assistant_url.rstrip("/")
        + "/api/services/notify/"
        + s.home_assistant_notify_service
    )
    payload: dict = {"title": title, "message": message}
    if url:
        payload["data"] = {"clickAction": url}
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
        log.info("notify · sent title=%r", title)
    except Exception as exc:  # noqa: BLE001 — never let notify break the caller
        log.warning("home assistant notify failed: %s", exc)


async def notify_task_created(task, raw) -> None:
    """Notify on a newly extracted task. `task` is an ORM Task, `raw` a RawInput."""
    meta = raw.source_metadata or {}
    parts: list[str] = []
    if task.due_date:
        parts.append(f"{_fmt_when(task.due_date)}")
    if task.estimation:
        parts.append(f"{task.estimation}min")
    details = " · ".join(parts)
    urls = meta.get("urls") or []
    await notify(
        title=f"{task.title[:100]}",
        message=details or "(no details)",
        url=urls[0] if urls else None,
    )


def _fmt_when(dt: datetime) -> str:
    """Short, human-friendly local-time format for phone notifications.

    Today  → just the time (e.g. "14:30").
    Else   → month/day + time, with year only when it's not the current one.
    """
    local = dt.astimezone()
    now = datetime.now(local.tzinfo)
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if local.year == now.year:
        return local.strftime("%b %d, %H:%M")
    return local.strftime("%b %d %Y, %H:%M")


async def notify_error(title: str, exc: BaseException, *, context: str | None = None) -> None:
    """Notify on a pipeline error. Includes the type, message, and a short tail
    of the traceback so the phone alert is actionable."""
    tb_tail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    body_lines = [tb_tail]
    if context:
        body_lines.append(context)
    await notify(title=title, message="\n".join(body_lines)[:512])
