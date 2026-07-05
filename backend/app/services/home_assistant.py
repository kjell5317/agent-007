"""Small Home Assistant service-call helpers.

Calls here are best-effort by design. HA integrations should never break the
calendar/task loops when HA is disabled or temporarily unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.timezones import user_tz

log = logging.getLogger(__name__)

_TIMEOUT = 5.0

# Lead time before the next event that the NIGHT action counts down to.
NEXT_EVENT_PREP_LEAD = timedelta(minutes=45)


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


async def get_next_event_datetime() -> datetime | None:
    """Read the configured HA input_datetime entity as an aware datetime.

    Returns None when HA isn't configured or the entity holds no usable value.
    The stored value is user-local wall time (see `set_next_event_datetime`),
    so we reattach the user timezone on the way back.
    """
    s = get_settings()
    entity_id = (s.home_assistant_next_event_entity_id or "").strip()
    if not s.home_assistant_url or not s.home_assistant_token or not entity_id:
        return None

    endpoint = s.home_assistant_url.rstrip("/") + f"/api/states/{entity_id}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                endpoint,
                headers={"Authorization": f"Bearer {s.home_assistant_token}"},
            )
            resp.raise_for_status()
            state = resp.json().get("state")
    except Exception as exc:  # noqa: BLE001 — HA is best-effort
        log.warning("home assistant input_datetime read failed: %s", exc)
        return None

    return _parse_home_assistant_datetime(state)


async def minutes_until_next_event_prep(now: datetime | None = None) -> int | None:
    """Minutes from `now` until `NEXT_EVENT_PREP_LEAD` before the next event.

    None when HA isn't configured or the entity holds no usable datetime — the
    caller must not treat that as "0 minutes left".
    """
    target = await get_next_event_datetime()
    if target is None:
        return None
    reference = now if now is not None else datetime.now(timezone.utc)
    return round((target - NEXT_EVENT_PREP_LEAD - reference).total_seconds() / 60)


def _parse_home_assistant_datetime(state: str | None) -> datetime | None:
    if not state or state in ("unknown", "unavailable"):
        return None
    try:
        naive = datetime.strptime(state, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=user_tz())
