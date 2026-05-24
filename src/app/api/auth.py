"""Login flow — Google SSO with an email allowlist.

Distinct from the Gmail data-access OAuth at /oauth/google/*: this flow only
verifies who the user is and sets a signed session cookie. It does not
persist any token. Reuses the same Google OAuth client (different redirect
URI, lighter scopes).
"""

from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

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

    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_login_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse(f"{_AUTH_URL}?{urlencode(params)}")


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    expires = _state_store.pop(state, None)
    if expires is None or expires < time.time():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired state")

    settings = get_settings()
    data = {
        "client_id": settings.google_oauth_client_id,
        "client_secret": settings.google_oauth_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.google_oauth_login_redirect_uri,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        token_resp = await client.post(_TOKEN_URL, data=data)
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        user_resp = await client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_resp.raise_for_status()
        userinfo = user_resp.json()

    email = (userinfo.get("email") or "").lower()
    if email not in settings.auth_allowed_emails:
        log.warning("login denied · email=%r not in allowlist", email)
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Access denied for {email!r}")

    request.session["email"] = email
    log.info("login ok · email=%s", email)
    return RedirectResponse("/")


@router.get("/whoami")
async def whoami(request: Request) -> dict:
    """Used by the UI to render identity / logout state."""
    email = request.session.get("email") if hasattr(request, "session") else None
    return {"email": email}


@router.post("/logout")
async def logout(request: Request) -> dict:
    email = request.session.get("email") if hasattr(request, "session") else None
    if hasattr(request, "session"):
        request.session.clear()
    if email:
        log.info("logout · email=%s", email)
    return {"ok": True}
