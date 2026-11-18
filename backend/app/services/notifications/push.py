"""Route a `Message` to the configured push provider.

Fire-and-forget: a failure here MUST NOT propagate to the caller — otherwise
a flaky notification service would turn into a flood of pipeline errors that
themselves want to fire notifications. We log and swallow.

Home Assistant is disabled when either `HOME_ASSISTANT_URL` or
`HOME_ASSISTANT_TOKEN` is empty — useful for local dev and CI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_TIMEOUT = 5.0
_CLEAR = "clear_notification"


@dataclass(frozen=True)
class Message:
    """`tag` lets the phone replace or clear an earlier push. `url` opens on
    tap. `sticky` keeps it after a tap; `importance` (`high`/`max`) +
    `channel` escalate to a heads-up alert; `persistent` makes it
    undismissable (Android, needs a `tag`)."""

    title: str
    body: str
    tag: str | None = None
    url: str | None = None
    actions: tuple[tuple[str, str], ...] = ()
    importance: str | None = None
    channel: str | None = None
    color: str | None = None
    sticky: bool = False
    persistent: bool = False

    @property
    def action_items(self) -> list[dict[str, str]]:
        return [{"action": action, "title": title} for action, title in self.actions]


async def send(message: Message) -> None:
    s = get_settings()
    if s.notification_provider == "none":
        return
    if s.notification_provider == "ntfy":
        await _send_ntfy(message)
        return
    await _send_home_assistant(message)


async def clear(tag: str) -> None:
    await send(Message(title="", body=_CLEAR, tag=tag))


async def _send_ntfy(message: Message) -> None:
    from app.services.notifications import ntfy

    s = get_settings()
    try:
        receipt = await ntfy.send(
            s, message.title, message.body, url=message.url, tag=message.tag,
            actions=message.action_items or None, importance=message.importance,
        )
        if message.body == _CLEAR:
            log.info("ntfy notify · cleared tag=%r server=%s id=%s", message.tag,
                     s.ntfy_base_url, (receipt or {}).get("id"))
        else:
            log.info("ntfy notify · accepted title=%r tag=%r server=%s id=%s",
                     message.title, message.tag, s.ntfy_base_url, (receipt or {}).get("id"))
    except Exception as exc:  # noqa: BLE001 — notification failures must not break task work
        log.warning("ntfy notify failed: %s", exc)


async def _send_home_assistant(message: Message) -> None:
    s = get_settings()
    if not s.home_assistant_url or not s.home_assistant_token:
        return
    endpoint = (
        s.home_assistant_url.rstrip("/") + "/api/services/notify/" + s.home_assistant_notify_service
    )
    data: dict[str, Any] = {}
    if message.url:
        data["clickAction"] = message.url
    if message.tag:
        data["tag"] = message.tag
    if message.actions:
        data["actions"] = message.action_items
    if message.sticky:
        data["sticky"] = "true"
    if message.importance:
        data["importance"] = message.importance
    if message.channel:
        data["channel"] = message.channel
    if message.color:
        data["color"] = message.color
    if message.persistent:
        # Undismissable — must be paired with a tag so the app can key it, and
        # sticky so an action tap doesn't clear it out from under us.
        data["persistent"] = "true"
        data["sticky"] = "true"
    payload: dict[str, Any] = {"title": message.title, "message": message.body}
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
        log.info("notify · sent title=%r tag=%r", message.title, message.tag)
    except Exception as exc:  # noqa: BLE001 — never let notify break the caller
        log.warning("home assistant notify failed: %s", exc)
