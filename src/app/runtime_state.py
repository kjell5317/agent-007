"""In-memory app runtime state.

Single-process toggles that intentionally do not persist — they reset to
their defaults on every process restart. Anything that needs to survive a
restart belongs in the database, not here.
"""

auto_poll_enabled: bool = True
