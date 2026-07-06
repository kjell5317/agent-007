"""Handler for reading today's sleep interval from Google Health."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.services.health.client import authorized_client
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SleepSegment:
    start: datetime
    end: datetime
    sleep_type: str | None
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
    payload = await client.list_sleep(start=start, end=end)
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
    for point in payload.get("dataPoints", []):
        try:
            segment = _normalize_session(point)
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


def _normalize_session(point: dict[str, Any]) -> SleepSegment:
    sleep = point["sleep"]
    interval = sleep["interval"]
    return SleepSegment(
        start=_parse_rfc3339(interval["startTime"]),
        end=_parse_rfc3339(interval["endTime"]),
        sleep_type=sleep.get("type"),
        raw=point,
    )


_RFC3339 = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})$"
)


def _parse_rfc3339(value: str) -> datetime:
    """Parse an RFC3339 timestamp, tolerating Google's nanosecond precision.

    `datetime.fromisoformat` accepts at most 6 fractional digits, so trim any
    extra before handing it over.
    """
    match = _RFC3339.match(value.strip())
    if match is None:
        raise ValueError(f"unrecognized RFC3339 timestamp: {value!r}")
    frac = match.group("frac")
    offset = "+00:00" if match.group("tz") == "Z" else match.group("tz")
    iso = match.group("base") + (f".{frac[:6]}" if frac else "") + offset
    return datetime.fromisoformat(iso)
