"""Generic OAuth routes.

Dispatches to whichever provider is registered under the URL segment.
No provider-specific code lives here.

`?app=<name>` selects a specific OAuth client when a provider has more than
one (e.g. Slack apps, which are workspace-scoped). The app name is stashed
in the state token so the callback can use the same client_secret for the
code exchange.
"""

import secrets
import time
from typing import NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_provider
from app.auth.base import OAuthProvider
from app.config import get_settings
from app.db import get_session
from app.db.models.oauth_token import OAuthToken
from app.db.clients import oauth_tokens

router = APIRouter(prefix="/oauth", tags=["oauth"])


class _State(NamedTuple):
    provider: str
    app_name: str | None
    expires_at: float


# In-process CSRF state. Lost on restart; fine for personal use on localhost.
_STATE_TTL_SECONDS = 600
_state_store: dict[str, _State] = {}


def _prune_state() -> None:
    now = time.time()
    for key in [k for k, s in _state_store.items() if s.expires_at < now]:
        _state_store.pop(key, None)


def _redirect_uri(provider: str) -> str:
    s = get_settings()
    if provider == "google":
        return s.google_oauth_redirect_uri
    if provider == "slack":
        return s.slack_oauth_redirect_uri
    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"No redirect URI configured for {provider!r}")


def _build_provider(provider: str, app_name: str | None) -> OAuthProvider:
    """Instantiate a provider, passing the app selector to those that need it."""
    provider_cls = get_provider(provider)
    if provider == "slack":
        return provider_cls(app_name=app_name)  # type: ignore[call-arg]
    return provider_cls()


@router.get("/{provider}/authorize")
async def authorize(
    provider: str,
    app: str | None = Query(None, description="Optional app selector — required when a provider has multiple OAuth clients configured (e.g. Slack)."),
) -> RedirectResponse:
    try:
        get_provider(provider)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown provider {provider!r}")

    _prune_state()
    state = secrets.token_urlsafe(32)
    _state_store[state] = _State(provider, app, time.time() + _STATE_TTL_SECONDS)

    try:
        provider_instance = _build_provider(provider, app)
        url = provider_instance.authorize_url(state=state, redirect_uri=_redirect_uri(provider))
    except RuntimeError as exc:
        _state_store.pop(state, None)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    return RedirectResponse(url)


@router.get("/{provider}/callback")
async def callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    session: Session = Depends(get_session),
) -> dict:
    try:
        get_provider(provider)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown provider {provider!r}")

    issued = _state_store.pop(state, None)
    if issued is None or issued.provider != provider or issued.expires_at < time.time():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired state")

    provider_instance = _build_provider(provider, issued.app_name)
    bundle = await provider_instance.exchange_code(code=code, redirect_uri=_redirect_uri(provider))
    account_key = await provider_instance.identify(bundle.access_token)

    oauth_tokens.upsert(
        session,
        provider=provider,
        account_key=account_key,
        bundle=bundle,
    )
    session.commit()
    return {
        "provider": provider,
        "app": issued.app_name,
        "account_key": account_key,
        "scopes": bundle.scopes,
    }


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
