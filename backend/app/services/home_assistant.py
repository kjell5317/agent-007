"""Small Home Assistant service-call helpers.

Calls here are best-effort by design. HA integrations should never break the
calendar/task loops when HA is disabled or temporarily unavailable.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_TIMEOUT = 5.0


def next_event_datetime_configured() -> bool:
    """Whether the HA input_datetime target has enough config to call."""
    s = get_settings()
    entity_id = (s.home_assistant_next_event_entity_id or "").strip()
    return bool(
        s.home_assistant_url
        and s.home_assistant_token
        and entity_id
    )


async def set_next_event_datetime(value: str) -> None:
    """Set the configured HA input_datetime entity to `value`.

    `value` must be in Home Assistant's expected local format:
    `YYYY-MM-DD HH:MM:SS`.
    """
    s = get_settings()
    entity_id = (s.home_assistant_next_event_entity_id or "").strip()
    if not s.home_assistant_url or not s.home_assistant_token or not entity_id:
        return

    endpoint = s.home_assistant_url.rstrip("/") + "/api/services/input_datetime/set_datetime"
    payload = {"entity_id": entity_id, "datetime": value}
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
        log.info("home assistant input_datetime · set %s=%s", entity_id, value)
    except Exception as exc:  # noqa: BLE001 — HA is best-effort
        log.warning("home assistant input_datetime failed: %s", exc)
