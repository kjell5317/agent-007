"""In-process pub/sub for server-sent events.

This works *because the app is a single process*: the API, the in-process
task-creation worker, and the ingestion poller all share one event loop, so a
plain set of `asyncio.Queue`s fans every mutation out to every connected
browser with no broker. The moment a second uvicorn worker or an out-of-process
RQ worker is introduced (see CLAUDE.md), this has to move behind Redis pub/sub —
publishers in another process would never reach these queues.

Subscribers receive already-serialized JSON strings; see `app.events.publish`.
"""

from __future__ import annotations

import logging

import asyncio

log = logging.getLogger(__name__)

# Per-subscriber buffer. A browser that stalls (backgrounded tab, dead network
# that EventSource hasn't given up on yet) fills its queue; we drop events for
# that one client rather than blocking publishers or growing without bound. The
# client reconciles via the focus/visibility refetch when it comes back.
_QUEUE_MAXSIZE = 1000

_subscribers: set[asyncio.Queue[str]] = set()


def subscribe() -> asyncio.Queue[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue[str]) -> None:
    _subscribers.discard(queue)


def publish(payload: str) -> None:
    for queue in _subscribers:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning("sse subscriber queue full; dropping event")
