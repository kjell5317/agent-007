"""Server-sent events stream.

A single `GET /events` connection per browser replaces the frontend's task /
inbox polling. Mutations elsewhere publish through `app.events.bus`; this
endpoint just drains one subscriber queue onto the wire. `sse-starlette` owns
the keep-alive ping and tears the generator down on client disconnect, at which
point we unsubscribe.
"""

from __future__ import annotations

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from app.events import bus

router = APIRouter(tags=["events"])


@router.get("/events")
async def events() -> EventSourceResponse:
    queue = bus.subscribe()

    async def stream():
        try:
            while True:
                payload = await queue.get()
                yield {"data": payload}
        finally:
            bus.unsubscribe(queue)

    return EventSourceResponse(stream())
