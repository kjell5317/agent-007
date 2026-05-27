"""Google Calendar service.

Two layers, both in `events.py`:

  * Generic API ops — `list_week_events`, `create_event`, `patch_event`,
    `delete_event`.
  * Task-mirror helpers — `add_task_event`, `update_task_event`,
    `delete_task_event`. Pure CRUD against the calendar event that
    mirrors a task; callers supply the planned `(start, end)`.

`discover.py` handles externally-edited events (cursor-based polling).
"""

from app.services.calendar.client import CalendarEvent, GoogleCalendarClient
from app.services.calendar.events import (
    KIND_COMMUTE,
    KIND_TASK,
    MANAGED_BY,
    WINDOW_DAYS,
    add_task_event,
    commute_private_properties,
    create_event,
    delete_event,
    delete_task_event,
    get_event,
    is_commute_event,
    is_managed_event,
    is_task_event,
    list_events_between,
    list_week_events,
    patch_event,
    private_properties,
    task_private_properties,
    update_task_event,
)

__all__ = [
    "CalendarEvent",
    "GoogleCalendarClient",
    "KIND_COMMUTE",
    "KIND_TASK",
    "MANAGED_BY",
    "WINDOW_DAYS",
    "add_task_event",
    "commute_private_properties",
    "create_event",
    "delete_event",
    "delete_task_event",
    "get_event",
    "is_commute_event",
    "is_managed_event",
    "is_task_event",
    "list_events_between",
    "list_week_events",
    "patch_event",
    "private_properties",
    "task_private_properties",
    "update_task_event",
]
