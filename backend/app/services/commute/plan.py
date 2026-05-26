"""Plan commute for a single calendar event.

Placeholder. Should expose a function that takes one event plus its
prev/next neighbours and returns the `(outbound, inbound)` plan pair —
recursing into the neighbours when a chain of online events shifts
the inbound leg.

Today this logic is the inner loop of `planner.plan_week_commutes`;
splitting it lets callers (the agent, ad-hoc API trigger, drag-and-drop
event handler) re-plan one event without redoing the whole week.
"""

from __future__ import annotations
