"""Small Home Assistant service-call helpers.

Calls here are best-effort by design. HA integrations should never break the
calendar/task loops when HA is disabled or temporarily unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

from app.config import get_settings
from app.db import SessionLocal
from app.services.health import request_awake_minutes
from app.services.points import adjust_points
from app.timezones import user_tz

log = logging.getLogger(__name__)

_TIMEOUT = 5.0

# Lead time before the next event that the NIGHT action counts down to.
NEXT_EVENT_PREP_LEAD = timedelta(minutes=45)

# Wait for Google Health data to sync after the DAY action before requesting it.
DAY_HEALTH_SYNC_DELAY_S = 5 * 60


class BackgroundTaskScheduler(Protocol):
    def add_task(self, func, *args, **kwargs) -> None: ...


def schedule_day_action(background_tasks: BackgroundTaskScheduler | None = None) -> datetime:
    """Queue the DAY action worker and return the captured action timestamp."""
    action_at = datetime.now(timezone.utc)
    if background_tasks is None:
        asyncio.create_task(process_day_action(action_at), name="day-health-sync")
    else:
        background_tasks.add_task(process_day_action, action_at)
    log.info("notify action · day queued action_at=%s", action_at.isoformat())
    return action_at


async def process_day_action(action_at: datetime) -> None:
    """Wait for Google Health sync, then deduct points for awake minutes."""
    if action_at.tzinfo is None:
        action_at = action_at.replace(tzinfo=timezone.utc)
    else:
        action_at = action_at.astimezone(timezone.utc)

    await asyncio.sleep(DAY_HEALTH_SYNC_DELAY_S)

    session = SessionLocal()
    try:
        awake_minutes = await request_awake_minutes(session, now=action_at)
        penalty = max(0, awake_minutes)
        if penalty:
            adjust_points(session, -penalty, caller="day", reason=f"Awake {awake_minutes} min")
        log.info(
            "notify action · day awake_minutes=%s points_deducted=%s",
            awake_minutes,
            penalty,
        )
    finally:
        session.close()


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
