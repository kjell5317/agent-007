"""Generic OAuth routes.

Dispatches to whichever provider is registered under the URL segment.
No provider-specific code lives here.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_session

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/{provider}/authorize")
async def authorize(provider: str, request: Request) -> dict:
    # TODO: look up provider via app.auth.get_provider(provider)
    # TODO: mint a CSRF `state` token, store it server-side (session/Redis), then redirect
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"authorize {provider!r} not implemented")


@router.get("/{provider}/callback")
async def callback(provider: str, request: Request, session: Session = Depends(get_session)) -> dict:
    # TODO: verify `state` matches what was issued by /authorize
    # TODO: exchange code via provider.exchange_code(...)
    # TODO: identify account via provider.identify(access_token)
    # TODO: encrypt and upsert into oauth_tokens
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"callback {provider!r} not implemented")


@router.post("/{provider}/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(provider: str, account_key: str, session: Session = Depends(get_session)) -> None:
    # TODO: revoke upstream where supported, delete oauth_tokens row
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"disconnect {provider!r} not implemented")
