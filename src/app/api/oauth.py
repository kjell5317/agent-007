"""Generic OAuth routes.

Dispatches to whichever provider is registered under the URL segment.
No provider-specific code lives here.
"""

import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_provider
from app.config import get_settings
from app.db import get_session
from app.models.oauth_token import OAuthToken
from app.storage import oauth_tokens

router = APIRouter(prefix="/oauth", tags=["oauth"])

# In-process CSRF state: {state_token: (provider, expires_at_unix)}.
# Lost on restart; fine for personal use on localhost. Move to Redis if
# multiple workers or cross-restart durability is ever needed.
_STATE_TTL_SECONDS = 600
_state_store: dict[str, tuple[str, float]] = {}


def _prune_state() -> None:
    now = time.time()
    for key in [k for k, (_, exp) in _state_store.items() if exp < now]:
        _state_store.pop(key, None)


def _redirect_uri(provider: str) -> str:
    s = get_settings()
    if provider == "google":
        return s.google_oauth_redirect_uri
    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No redirect URI configured for {provider!r}")


@router.get("/{provider}/authorize")
async def authorize(provider: str) -> RedirectResponse:
    try:
        provider_cls = get_provider(provider)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown provider {provider!r}")

    _prune_state()
    state = secrets.token_urlsafe(32)
    _state_store[state] = (provider, time.time() + _STATE_TTL_SECONDS)

    url = provider_cls().authorize_url(state=state, redirect_uri=_redirect_uri(provider))
    return RedirectResponse(url)


@router.get("/{provider}/callback")
async def callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    session: Session = Depends(get_session),
) -> dict:
    try:
        provider_cls = get_provider(provider)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown provider {provider!r}")

    issued = _state_store.pop(state, None)
    if issued is None or issued[0] != provider or issued[1] < time.time():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired state")

    provider_instance = provider_cls()
    bundle = await provider_instance.exchange_code(code=code, redirect_uri=_redirect_uri(provider))
    account_key = await provider_instance.identify(bundle.access_token)

    oauth_tokens.upsert(
        session,
        provider=provider,
        account_key=account_key,
        bundle=bundle,
    )
    session.commit()
    return {"provider": provider, "account_key": account_key, "scopes": bundle.scopes}


@router.post("/{provider}/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    provider: str,
    account_key: str = Query(...),
    session: Session = Depends(get_session),
) -> None:
    row = (
        session.query(OAuthToken)
        .filter(OAuthToken.provider == provider, OAuthToken.account_key == account_key)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such token")
    session.delete(row)
    session.commit()
