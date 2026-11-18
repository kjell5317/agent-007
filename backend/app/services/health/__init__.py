"""Google health integrations.

`request_awake_minutes` is consumed by the notifications router (the HA morning
`DAY` action); `record_last_night` by the hourly sleep-context job, which keeps
one row of last night's sleep per day for learning when work gets done.
"""

from app.services.health.sleep import (
    SleepInterval,
    SleepSegment,
    record_last_night,
    request_awake_minutes,
    request_todays_sleep_interval,
)

__all__ = [
    "SleepInterval",
    "SleepSegment",
    "record_last_night",
    "request_awake_minutes",
    "request_todays_sleep_interval",
]
