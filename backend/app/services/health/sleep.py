"""Handler for reading today's sleep interval from Google Fit."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.services.health.client import SLEEP_SEGMENT_DATA_TYPE, authorized_client
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SleepSegment:
    start: datetime
    end: datetime
    sleep_stage: int | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class SleepInterval:
    start: datetime
    end: datetime
    segments: list[SleepSegment]
    raw: dict[str, Any]


async def request_todays_sleep_interval(
    session: Session,
    *,
    account_key: str | None = None,
    now: datetime | None = None,
) -> SleepInterval | None:
    """Fetch and normalize the user's sleep interval for today's local day."""
    start, end = _today_bounds(now)
    client = await authorized_client(session, account_key)
    payload = await client.aggregate_sleep_segments(start=start, end=end)
    return normalize_sleep_interval(payload)


async def request_awake_minutes(
    session: Session,
    *,
    account_key: str | None = None,
    now: datetime | None = None,
) -> int:
    """Minutes elapsed since last night's sleep ended; 0 when nothing is recorded."""
    reference = now if now is not None else datetime.now(timezone.utc)
    interval = await request_todays_sleep_interval(
        session, account_key=account_key, now=reference
    )
    if interval is None:
        log.info("google sleep · none returned for today's local day")
        return 0
    minutes = round((reference - interval.end).total_seconds() / 60)
    log.info(
        "google sleep · start=%s end=%s segments=%d awake_minutes=%s",
        to_user_tz(interval.start).isoformat(timespec="minutes"),
        to_user_tz(interval.end).isoformat(timespec="minutes"),
        len(interval.segments),
        minutes,
    )
    return minutes


def normalize_sleep_interval(payload: dict[str, Any]) -> SleepInterval | None:
    segments: list[SleepSegment] = []
    for point in _sleep_points(payload):
        try:
            segment = _normalize_point(point)
        except (KeyError, TypeError, ValueError):
            continue
        if segment.end > segment.start:
            segments.append(segment)

    if not segments:
        return None

    segments.sort(key=lambda segment: segment.start)
    return SleepInterval(
        start=min(segment.start for segment in segments),
        end=max(segment.end for segment in segments),
        segments=segments,
        raw=payload,
    )


def _today_bounds(now: datetime | None) -> tuple[datetime, datetime]:
    tz = user_tz()
    if now is None:
        local_now = datetime.now(tz)
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local_now = now.astimezone(tz)

    start = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _sleep_points(payload: dict[str, Any]):
    for bucket in payload.get("bucket", []):
        for dataset in bucket.get("dataset", []):
            for point in dataset.get("point", []):
                if point.get("dataTypeName", SLEEP_SEGMENT_DATA_TYPE) != SLEEP_SEGMENT_DATA_TYPE:
                    continue
                yield point


def _normalize_point(point: dict[str, Any]) -> SleepSegment:
    return SleepSegment(
        start=_nanos_to_datetime(point["startTimeNanos"]),
        end=_nanos_to_datetime(point["endTimeNanos"]),
        sleep_stage=_sleep_stage(point),
        raw=point,
    )


def _sleep_stage(point: dict[str, Any]) -> int | None:
    values = point.get("value") or []
    if not values:
        return None
    first = values[0]
    if not isinstance(first, dict) or "intVal" not in first:
        return None
    return int(first["intVal"])


def _nanos_to_datetime(value: str | int) -> datetime:
    seconds, nanos = divmod(int(value), 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(microsecond=nanos // 1000)
