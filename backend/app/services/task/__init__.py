"""Task service: create / update / close / dismiss / open / reopen / queue.

Each action gets its own module so the router stays a thin dispatch
layer. Services raise:

  * `LookupError` — when a task or its anchor raw_input doesn't exist.
    Routers translate this to HTTP 404.
  * `ValueError`  — when the input is well-formed but rejected for a
    business reason. Routers translate this to HTTP 400.

Anything else bubbles up as a 500.
"""

from app.services.task import queue

__all__ = ["queue"]
