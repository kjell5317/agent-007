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
from app.events import publish_task
from app.services.health import request_awake_minutes
from app.services.home_assistant import minutes_until_next_event_prep
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


def _check_secret(request: Request) -> None:
    expected = get_settings().home_assistant_action_secret
    if not expected:
        return
    provided = request.headers.get("x-notify-secret") or request.query_params.get("secret")
    if provided != expected:
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


@router.post("/actions", status_code=status.HTTP_202_ACCEPTED)
async def handle_action(
    payload: ActionPayload,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    _check_secret(request)

    if payload.action == ACTION_DAY:
        awake_minutes = await request_awake_minutes(session)
        penalty = max(0, awake_minutes * 2)
        if penalty:
            adjust_points(session, -penalty, caller="day", reason=f"awake {awake_minutes} min")
        log.info(
            "notify action · day awake_minutes=%s points_deducted=%s",
            awake_minutes,
            penalty,
        )
        return {
            "ok": True,
            "action": payload.action,
            "awake_minutes": awake_minutes,
            "points_deducted": penalty,
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
                    caller="night",
                    reason=f"{shortfall} min under 8h",
                )
        log.info(
            "notify action · night minutes_until_prep=%s points_deducted=%s",
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
