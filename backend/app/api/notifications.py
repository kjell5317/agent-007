"""Webhook endpoint for Home Assistant action callbacks.

The HA companion app sends `mobile_app_notification_action` events when
the user taps an action button on one of our notifications. The user is
expected to wire an HA automation that POSTs the relevant event fields
here so we can act on the tap.

Auth: `HOME_ASSISTANT_ACTION_SECRET`. When set, the request must include
it via either `X-Notify-Secret` header or `?secret=` query param. When
empty (local dev) the check is skipped.

The endpoint is exempt from the email-allowlist middleware — HA has no
session — so the shared secret IS the auth in production.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.db.clients import tasks as tasks_store
from app.services.notify import ACTION_EXTEND_WINDOW
from app.services.plan.schedule import schedule

log = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


class ActionPayload(BaseModel):
    action: str
    # HA event includes `tag` — we encode the task id as `task-<uuid>`.
    tag: str | None = None
    # Alternatively, the HA automation can pass `task_id` directly.
    task_id: str | None = None


def _check_secret(request: Request) -> None:
    expected = get_settings().home_assistant_action_secret
    if not expected:
        return
    provided = (
        request.headers.get("x-notify-secret")
        or request.query_params.get("secret")
    )
    if provided != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid notify secret")


def _resolve_task_id(payload: ActionPayload) -> uuid.UUID:
    raw = payload.task_id
    if not raw and payload.tag and payload.tag.startswith("task-"):
        raw = payload.tag[len("task-"):]
    if not raw:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "task_id or tag=task-<uuid> required",
        )
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid task id") from exc


@router.post("/actions", status_code=status.HTTP_202_ACCEPTED)
async def handle_action(
    payload: ActionPayload,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    _check_secret(request)
    task_id = _resolve_task_id(payload)
    task = tasks_store.get(session, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")

    if payload.action == ACTION_EXTEND_WINDOW:
        log.info("notify action · extend_window task=%s", task.id)
        await schedule(session, task, extend_window=True)
        return {"ok": True, "action": payload.action, "task_id": str(task.id)}

    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"unknown action: {payload.action}",
    )
