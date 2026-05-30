"""Inbound webhooks for push-based ingestion sources.

Currently WhatsApp Business (Meta Cloud API), which has no polling API for
received messages — this endpoint is its only ingestion path.

Auth: Meta signs every POST with `X-Hub-Signature-256` (HMAC-SHA256 over the
raw body, keyed by the app secret). We verify that before parsing anything,
per the house rule. The endpoint is exempt from the email-allowlist middleware
— Meta has no session — so the signature IS the auth in production.

The handler stays tiny and fast: verify, parse, hand off to a background task,
and 200 immediately. Meta retries (and eventually disables) webhooks that are
slow or non-200, so the agent run — which makes an LLM call — must not block
the response.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.db import SessionLocal
from app.services.input.create import drain
from app.services.input.whatsapp import ParsedMessage, WhatsAppSource, parse_messages

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify_whatsapp(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Meta's subscription handshake: echo `hub.challenge` when the verify
    token matches, so Meta marks the webhook subscription as verified."""
    expected = get_settings().whatsapp_verify_token
    if mode == "subscribe" and expected and token == expected:
        return PlainTextResponse(challenge or "")
    return PlainTextResponse("forbidden", status_code=status.HTTP_403_FORBIDDEN)


@router.post("/whatsapp")
async def receive_whatsapp(request: Request, background: BackgroundTasks) -> Response:
    settings = get_settings()
    raw_body = await request.body()

    secret = settings.whatsapp_app_secret
    if secret and not _valid_signature(
        raw_body, request.headers.get("x-hub-signature-256"), secret
    ):
        log.warning("whatsapp webhook · rejected: bad signature")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    payload = await request.json()
    messages = parse_messages(payload)
    if messages:
        log.info("whatsapp webhook · accepted %d message(s)", len(messages))
        background.add_task(_ingest, messages)
    # Always 200 fast — Meta retries and eventually disables slow/erroring
    # webhooks. Status-only callbacks (delivery/read receipts) parse to zero
    # messages and fall through to here.
    return Response(status_code=status.HTTP_200_OK)


def _valid_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :])


async def _ingest(messages: list[ParsedMessage]) -> None:
    """Run the parsed messages through the shared ingestion pipeline on a
    fresh session — the request session is gone once we've returned 200."""
    with SessionLocal() as session:
        await drain(WhatsAppSource(messages=messages), session)
