"""In-memory app runtime state.

Single-process toggles that intentionally do not persist — they reset to
their defaults on every process restart. Anything that needs to survive a
restart belongs in the database, not here.
"""

from datetime import datetime, timezone

auto_poll_enabled: bool = True

# Watermark for the inbox unread indicator. Initialised at process start so
# counts/dots are 0 on first load and rise as new rows land before the user
# opens the inbox; reset to "now" whenever the inbox is viewed.
last_seen_input_at: datetime = datetime.now(timezone.utc)
