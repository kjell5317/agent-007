"""Google health integrations.

`request_awake_minutes` is consumed by the notifications router (the HA morning
`DAY` action). The rest stays standalone — nothing else in cron, agent tools,
scheduling, or the frontend reaches in here.
"""

from app.services.health.sleep import (
    SleepInterval,
    SleepSegment,
    request_awake_minutes,
    request_todays_sleep_interval,
)

__all__ = [
    "SleepInterval",
    "SleepSegment",
    "request_awake_minutes",
    "request_todays_sleep_interval",
]
