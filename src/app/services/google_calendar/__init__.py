"""Google Calendar service.

Two public operations the rest of the app cares about:

  * `list_week_events(...)` — fan out across one or more calendars and return
    every event in a 7-day window that starts at any timezone-aware datetime
    the caller passes.
  * `create_event(...)` / `patch_event(...)` — insert or update events on a
    single target calendar.

Plus the task-mirror layer used by the agent + task endpoints:

  * `add_task_to_calendar(session, task)` — create an event when a task is born.
  * `update_task_in_calendar(session, task)` — keep the event in sync on edit.
"""

from app.services.google_calendar.client import CalendarEvent, GoogleCalendarClient
from app.services.google_calendar.events import (
    WINDOW_DAYS,
    create_event,
    delete_event,
    list_week_events,
    patch_event,
)
from app.services.google_calendar.sync import (
    add_task_to_calendar,
    remove_task_from_calendar,
    update_task_in_calendar,
)

__all__ = [
    "CalendarEvent",
    "GoogleCalendarClient",
    "WINDOW_DAYS",
    "add_task_to_calendar",
    "create_event",
    "delete_event",
    "list_week_events",
    "patch_event",
    "remove_task_from_calendar",
    "update_task_in_calendar",
]
