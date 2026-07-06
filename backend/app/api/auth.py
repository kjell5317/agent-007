"""Login flow — Google SSO that also captures Google integration tokens.

One Google consent screen, two outcomes:

  1. A signed session cookie keyed by email (used by `AuthMiddleware`).
  2. A persisted token bundle in `oauth_tokens` for the same account, so the
     Gmail ingestion source and Calendar service can call Google APIs without
     a separate authorization step.

Reuses `GoogleOAuthProvider`, which requests openid email · gmail.readonly ·
calendar.events with offline access. The allowlist is enforced before the
bundle is persisted — denied emails leave no trace in the database.

Sleep is NOT captured here: the Google Health API rejects tokens that also
carry Gmail/Calendar scopes, so it has its own health-only grant — visit
`/oauth/google_health/authorize` once to connect it.

The provider-agnostic `/oauth/google/*` routes still work for re-authorizing
after a scope change; this flow just removes the need to visit them.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_provider
from app.auth.google_tokens import GoogleTokenError, get_fresh_google_token
from app.config import get_settings
from app.db import get_session
from app.db.clients import oauth_tokens

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 600
_state_store: dict[str, float] = {}


def _prune_state() -> None:
    now = time.time()
    for key, exp in list(_state_store.items()):
        if exp < now:
            _state_store.pop(key, None)


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    settings = get_settings()
    if not settings.auth_allowed_emails:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Auth is disabled: set AUTH_ALLOWED_EMAILS in .env to enable.",
        )
    if not settings.google_oauth_client_id:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "GOOGLE_OAUTH_CLIENT_ID is not configured.",
        )

    if (request.session.get("email") or "").lower() in settings.auth_allowed_emails:
        return RedirectResponse("/")

    _prune_state()
    state = secrets.token_urlsafe(32)
    _state_store[state] = time.time() + _STATE_TTL_SECONDS

    provider = get_provider("google")()
    url = provider.authorize_url(
        state=state, redirect_uri=settings.google_oauth_login_redirect_uri,
    )
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    expires = _state_store.pop(state, None)
    if expires is None or expires < time.time():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired state")

    settings = get_settings()
    provider = get_provider("google")()
    bundle = await provider.exchange_code(
        code=code, redirect_uri=settings.google_oauth_login_redirect_uri,
    )
    email = (await provider.identify(bundle.access_token)).lower()

    if email not in settings.auth_allowed_emails:
        log.warning("login denied · email=%r not in allowlist", email)
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Access denied for {email!r}")

    # Persist the full token bundle so the same consent covers Gmail ingestion,
    # the Calendar service, and health/sleep reads. extra_merge preserves any
    # per-source state already stored under this account (e.g. Gmail's
    # history_id watermark).
    existing = oauth_tokens.get_decrypted(session, provider="google", account_key=email)
    oauth_tokens.upsert(
        session,
        provider="google",
        account_key=email,
        bundle=bundle,
        extra_merge=existing.extra if existing else None,
    )
    session.commit()

    request.session["email"] = email
    log.info("login ok · email=%s scopes=%s", email, " ".join(bundle.scopes))
    return RedirectResponse("/")


@router.get("/whoami")
async def whoami(request: Request, session: Session = Depends(get_session)) -> dict:
    """Used by the UI to render identity / logout state."""
    email = request.session.get("email") if hasattr(request, "session") else None
    if not email:
        return {"email": None}

    settings = get_settings()
    account_key = email.lower()
    if settings.auth_allowed_emails and account_key not in settings.auth_allowed_emails:
        request.session.clear()
        return {"email": None}

    if settings.auth_allowed_emails:
        try:
            await get_fresh_google_token(session, account_key=account_key)
        except GoogleTokenError as exc:
            log.info("session reauthorization required · email=%s reason=%s", account_key, exc)
            request.session.clear()
            return {"email": None}

    return {"email": email}


@router.post("/logout")
async def logout(request: Request) -> dict:
    email = request.session.get("email") if hasattr(request, "session") else None
    if hasattr(request, "session"):
        request.session.clear()
    if email:
        log.info("logout · email=%s", email)
    return {"ok": True}
