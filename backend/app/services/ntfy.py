"""ntfy transport for the app's provider-neutral notification messages."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from app.config.settings import Settings

ACTION_LIFETIME_SECONDS = 30 * 24 * 60 * 60
TIMEOUT_SECONDS = 5.0


def action_secret(settings: Settings) -> str:
    return settings.notify_action_secret or settings.home_assistant_action_secret


def signed_action(action: str, tag: str, secret: str, *, now: int | None = None) -> dict[str, Any]:
    expires = (int(time.time()) if now is None else now) + ACTION_LIFETIME_SECONDS
    message = f"{action}\n{tag}\n{expires}".encode()
    return {
        "action": action,
        "tag": tag,
        "expires": expires,
        "signature": hmac.new(secret.encode(), message, hashlib.sha256).hexdigest(),
    }


def verify_action(
    action: str, tag: str | None, expires: int | None, signature: str | None, secret: str,
) -> bool:
    if not secret or not tag or expires is None or not signature or expires < time.time():
        return False
    message = f"{action}\n{tag}\n{expires}".encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def sequence_id(tag: str) -> str:
    return "007-" + hashlib.sha256(tag.encode()).hexdigest()[:24]


def action_url(settings: Settings) -> str:
    if settings.notification_action_url.strip():
        return settings.notification_action_url.strip()
    parts = urlsplit(settings.task_default_url)
    return urlunsplit((
        parts.scheme, parts.netloc,
        parts.path.rstrip("/") + "/notifications/actions", "", "",
    ))


def message_payload(
    settings: Settings, title: str, message: str, *, url: str | None,
    tag: str | None, actions: list[dict[str, str]] | None,
    importance: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "topic": settings.ntfy_topic.strip(),
        "title": title,
        "message": message,
        "priority": 5 if importance == "max" else 4 if importance == "high" else 3,
    }
    if url:
        payload["click"] = url
    if tag:
        payload["sequence_id"] = sequence_id(tag)
    if actions and tag:
        secret = action_secret(settings)
        callback = action_url(settings)
        payload["actions"] = [
            {
                "action": "http",
                "label": item["title"],
                "url": callback,
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(signed_action(item["action"], tag, secret)),
                "clear": True,
            }
            for item in actions
        ]
    return payload


async def send(
    settings: Settings, title: str, message: str, *, url: str | None,
    tag: str | None, actions: list[dict[str, str]] | None,
    importance: str | None,
) -> None:
    base = settings.ntfy_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.ntfy_token}"} if settings.ntfy_token else {}
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        if message == "clear_notification":
            if not tag:
                return
            endpoint = f"{base}/{quote(settings.ntfy_topic.strip(), safe='')}/{sequence_id(tag)}/clear"
            response = await client.put(endpoint, headers=headers)
        else:
            response = await client.post(
                base + "/",
                json=message_payload(
                    settings, title, message, url=url, tag=tag,
                    actions=actions, importance=importance,
                ),
                headers=headers,
            )
        response.raise_for_status()
