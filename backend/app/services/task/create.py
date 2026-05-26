"""Manual task creation flow.

The API hands an arbitrary `{title, description, …}` payload and the
work splits in two:

  1. Persist a synthetic `raw_input(source="manual")` *synchronously* so
     the API can return its id immediately. Polling `GET /inputs/{id}`
     tells the client when the task is ready.
  2. Hand the rest — agent-extract any missing fields, persist the task,
     mirror to Calendar — to the in-process queue worker.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models.raw_input import RawInput
from app.services.task.queue import enqueue


async def create_manual_task(session: Session, user_fields: dict[str, Any]) -> RawInput:
    """Anchor a manual raw_input and enqueue the task-creation worker.

    Returns the persisted RawInput so the router can hand its id back to
    the client. Raises `ValueError` when the payload has neither title
    nor description (nothing for the agent to chew on).
    """
    content = (user_fields.get("description") or user_fields.get("title") or "").strip()
    if not content:
        raise ValueError("Provide a title or description")

    raw = RawInput(
        source="manual",
        content=content,
        source_metadata={"manual": True},
        status="processing",
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)

    await enqueue(raw.id, user_fields)
    return raw
