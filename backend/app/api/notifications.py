"""Webhook endpoint for Home Assistant and ntfy action callbacks.

The HA companion app sends `mobile_app_notification_action` events when
the user taps an action button on one of our notifications. The user is
expected to wire an HA automation that POSTs the relevant event fields
here so we can act on the tap.

Home Assistant sends the shared secret in a header or query parameter. ntfy
buttons carry a scoped signed payload; the permanent secret stays server-side.

The endpoint is exempt from the email-allowlist middleware.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.db.clients import tasks as tasks_store
from app.events import publish_task
from app.services.home_assistant import minutes_until_next_event_prep, schedule_day_action
from app.services.kotx import client as kotx_client
from app.services.notify import (
    ACTION_CLOSE_TASK,
    ACTION_DAY,
    ACTION_DISMISS_TASK,
    ACTION_KOTX_APPROVE,
    ACTION_KOTX_COMMENT,
    ACTION_KOTX_MERGE,
    ACTION_KOTX_START,
    ACTION_NIGHT,
    ACTION_RESCHEDULE_TASK,
    clear_task_notification,
)
from app.services.ntfy import action_secret, verify_action
from app.services.plan.schedule import schedule_task, scheduled_interval_for
from app.services.points import adjust_points
from app.services.task.close import close_task as close_task_svc
from app.services.task.dismiss import dismiss_task

log = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

# NIGHT docks points for falling short of an 8h sleep target: the minutes below
# 8h until the next-event prep time, rounded to 10 min and divided by 10.
NIGHT_SLEEP_TARGET_MINUTES = 8 * 60

# HA action id → kotx client function name. Resolved via getattr at call time so
# the actual POST is dispatched (and stays patchable in tests).
_KOTX_ACTIONS = {
    ACTION_KOTX_START: "start_task",
    ACTION_KOTX_APPROVE: "approve_task",
    ACTION_KOTX_MERGE: "merge_task",
    ACTION_KOTX_COMMENT: "comment_task",
}


class ActionPayload(BaseModel):
    action: str
    # HA event includes `tag` — we encode the task id as `task-<uuid>`.
    tag: str | None = None
    # Alternatively, the HA automation can pass `task_id` directly.
    task_id: str | None = None
    expires: int | None = None
    signature: str | None = None


def _check_secret(request: Request, payload: ActionPayload) -> None:
    settings = get_settings()
    secrets = tuple(filter(None, (
        getattr(settings, "notify_action_secret", ""),
        settings.home_assistant_action_secret,
    )))
    if not secrets:
        return
    provided = request.headers.get("x-notify-secret") or request.query_params.get("secret")
    if provided in secrets:
        return
    if payload.task_id is None and verify_action(
        payload.action, payload.tag, payload.expires, payload.signature,
        action_secret(settings),
    ):
        return
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid notify secret")


def _resolve_task_id(payload: ActionPayload) -> uuid.UUID:
    raw = payload.task_id
    if not raw and payload.tag and payload.tag.startswith("task-"):
        raw = payload.tag[len("task-") :]
    if not raw:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "task_id or tag=task-<uuid> required",
        )
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid task id") from exc


def _ntfy_cors_origin(request: Request) -> str | None:
    settings = get_settings()
    if getattr(settings, "notification_provider", "home_assistant") != "ntfy":
        return None
    parts = urlsplit(settings.ntfy_base_url)
    allowed = f"{parts.scheme}://{parts.netloc}"
    origin = request.headers.get("origin")
    return allowed if origin == allowed else None


@router.options("/actions", include_in_schema=False)
def action_preflight(request: Request) -> Response:
    origin = _ntfy_cors_origin(request)
    if origin is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Origin not allowed")
    return Response(status_code=204, headers={
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    })


@router.post("/actions", status_code=status.HTTP_202_ACCEPTED)
async def handle_action(
    payload: ActionPayload,
    request: Request,
    response: Response = None,
    background_tasks: BackgroundTasks = None,
    session: Session = Depends(get_session),
) -> dict:
    _check_secret(request, payload)
    origin = _ntfy_cors_origin(request)
    if response is not None and origin is not None:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"

    if payload.action == ACTION_DAY:
        schedule_day_action(background_tasks)
        return {
            "ok": True,
            "action": payload.action,
            "queued": True,
        }

    if payload.action == ACTION_NIGHT:
        minutes = await minutes_until_next_event_prep()
        penalty = 0
        if minutes is not None:
            shortfall = NIGHT_SLEEP_TARGET_MINUTES - minutes
            # shortfall rounded to the nearest 10 min, then divided by 2.
            penalty = max(0, round(shortfall / 2))
            if penalty:
                adjust_points(
                    session,
                    -penalty,
                    caller="Night",
                    reason=f"Under 8h by {shortfall} min",
                )
        log.info(
            "notify action · Night minutes_until_prep=%s points_deducted=%s",
            minutes,
            penalty,
        )
        return {
            "ok": True,
            "action": payload.action,
            "minutes_until_prep": minutes,
            "points_deducted": penalty,
        }

    task_id = _resolve_task_id(payload)
    task = tasks_store.get(session, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")

    if payload.action == ACTION_CLOSE_TASK:
        log.info("notify action · close task=%s", task.id)
        await close_task_svc(session, task.id)
        return {"ok": True, "action": payload.action, "task_id": str(task.id)}

    if payload.action == ACTION_DISMISS_TASK:
        log.info("notify action · dismiss task=%s", task.id)
        await dismiss_task(session, task.id)
        return {"ok": True, "action": payload.action, "task_id": str(task.id)}

    if payload.action == ACTION_RESCHEDULE_TASK:
        log.info("notify action · reschedule task=%s", task.id)
        result = await schedule_task(session, task, block=scheduled_interval_for(task))
        if result is not None:
            publish_task(session, task.id)
        return {"ok": True, "action": payload.action, "task_id": str(task.id)}

    kotx_fn_name = _KOTX_ACTIONS.get(payload.action)
    if kotx_fn_name is not None:
        if task.kotx_task_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "task has no linked kotx run")
        # kotx's own transition webhook mirrors the resulting state back to us,
        # so there's nothing to publish here — the run section refreshes then.
        ok = await getattr(kotx_client, kotx_fn_name)(task.kotx_task_id)
        # The prompt proposed this action; once it's kicked off, clear it so a
        # stale button doesn't linger until the next transition replaces it.
        await clear_task_notification(task.id)
        log.info(
            "notify action · kotx %s task=%s kotx_id=%s ok=%s",
            payload.action,
            task.id,
            task.kotx_task_id,
            ok,
        )
        return {"ok": ok, "action": payload.action, "task_id": str(task.id)}

    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"unknown action: {payload.action}",
    )
