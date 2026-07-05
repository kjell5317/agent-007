"""Incoming webhooks.

  * POST /webhooks/kotx — kotx state-transition feed. Authenticated by
    `X-Kotx-Signature: sha256=<hex>` (HMAC-SHA256 over the raw body with
    KOTX_WEBHOOK_SECRET), NOT by the session middleware — the path is
    exempted in `app.auth.middleware`. Signature is verified before the
    payload is parsed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.config import get_settings
from app.db import SessionLocal
from app.events import publish_kotx
from app.services.input.create import drain
from app.services.input.kotx.source import KotxSource

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


async def _process_kotx_delivery(task: dict) -> None:
    """Drain one kotx transition off the request path.

    kotx's delivery timeout is shorter than a worst-case transition (brief
    fetch + LLM extraction + a scheduling sweep that exhausts both windows),
    so processing inline made kotx time out and *redeliver* — a second run
    that raced the first and clobbered its task status/notification. We ack
    fast and do the work here instead."""
    session = SessionLocal()
    try:
        summary = await drain(KotxSource([task]), session)
    finally:
        session.close()

    # Nudge connected browsers to refetch /kotx/tasks — every delivery means a
    # run changed upstream, whether or not it became an actionable inbox item.
    publish_kotx()

    log.info(
        "kotx webhook · task=%s state=%s fetched=%d errors=%d",
        task.get("id"), task.get("state"),
        summary["fetched"], len(summary["errors"]),
    )


@router.post("/kotx")
async def kotx_webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    settings = get_settings()
    if not settings.kotx_webhook_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "kotx webhook is not configured"
        )

    body = await request.body()
    if not _verify_signature(
        body, request.headers.get("x-kotx-signature"), settings.kotx_webhook_secret
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON") from exc

    task = payload.get("task") if isinstance(payload, dict) else None
    if not isinstance(task, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing task payload")

    # Verify + parse synchronously (fast, and the signature must gate access),
    # but process out of band so we ack well inside kotx's delivery timeout.
    background_tasks.add_task(_process_kotx_delivery, task)
    return {"ok": True}
