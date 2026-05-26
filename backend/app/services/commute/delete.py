"""Delete an existing commute event from the calendar.

Placeholder. Today this lives inside `planner._write_plans`, which
diffs the new plan set against existing `[commute]` events and deletes
the ones that no longer match. Future home for a single
`delete_commute(session, event_id)` that any code path (agent,
user-driven 'remove commute' button, batch planner) can call.
"""

from __future__ import annotations
