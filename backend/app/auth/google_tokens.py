"""Helpers for Google OAuth tokens used by app sessions and integrations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth.base import get_provider
from app.db.clients import oauth_tokens
from app.db.clients.oauth_tokens import DecryptedToken


class GoogleTokenError(RuntimeError):
    """Base class for Google token resolution failures."""


class GoogleTokenMissing(GoogleTokenError):
    """No stored Google token exists for the requested account."""


class GoogleReauthorizationRequired(GoogleTokenError):
    """The token cannot be refreshed without another Google consent flow."""


async def get_fresh_google_token(
    session: Session,
    *,
    account_key: str | None = None,
) -> DecryptedToken:
    """Return a usable Google token, refreshing and persisting it when expired."""
    token = oauth_tokens.get_decrypted(
        session,
        provider="google",
        account_key=account_key,
    )
    if token is None:
        raise GoogleTokenMissing("No Google account connected.")

    if not token.is_expired:
        return token

    if not token.refresh_token:
        raise GoogleReauthorizationRequired(
            "Google access token expired and no refresh_token available; re-authorize."
        )

    bundle = await get_provider("google")().refresh(token.refresh_token)
    oauth_tokens.upsert(
        session,
        provider="google",
        account_key=token.account_key,
        bundle=bundle,
        extra_merge=token.extra,
    )
    session.commit()

    refreshed = oauth_tokens.get_decrypted(
        session,
        provider="google",
        account_key=token.account_key,
    )
    if refreshed is None:
        raise GoogleTokenMissing("Google token disappeared after refresh.")
    return refreshed
