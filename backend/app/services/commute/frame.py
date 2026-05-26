"""Time-frame for a single event including its commute window.

Placeholder. Should expose a single function that, given one
`CalendarEvent`, returns the `(earliest_depart, latest_return)` interval
the event occupies once you account for travel to and from it — *without*
looking at neighbouring events. The "consider neighbours" logic belongs
in `plan.py` (single-event plan with prev/next look-up) and `batch.py`
(week-ahead pass).

Today the framing math is inlined in `planner._build_leg` /
`planner._effective_inbound_depart`; pulling it out here lets the agent
ask "how long is this event really?" without dragging the whole
batch planner along.
"""

from __future__ import annotations
