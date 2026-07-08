"""Notion MCP OAuth token resolution for the chat agent's MCP client.

Mirrors `app.auth.google_tokens`, but Notion's refresh needs the token endpoint,
client id, and resource captured during authorization — those live in the
token's `extra`, so refresh routes through `refresh_with_context`.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth.base import get_provider
from app.db.clients import oauth_tokens
from app.db.clients.oauth_tokens import DecryptedToken

PROVIDER = "notion"


class NotionTokenMissing(RuntimeError):
    """No Notion workspace is connected."""


class NotionReauthorizationRequired(RuntimeError):
    """The Notion token expired and cannot be refreshed; re-authorize."""


def is_connected(session: Session) -> bool:
    return oauth_tokens.get_decrypted(session, provider=PROVIDER) is not None


async def get_fresh_notion_token(session: Session) -> DecryptedToken:
    """Return a usable Notion access token, refreshing and persisting when expired."""
    token = oauth_tokens.get_decrypted(session, provider=PROVIDER)
    if token is None:
        raise NotionTokenMissing("No Notion workspace connected.")
    if not token.is_expired:
        return token
    if not token.refresh_token:
        raise NotionReauthorizationRequired(
            "Notion access token expired and no refresh_token available; re-authorize."
        )

    bundle = await get_provider(PROVIDER)().refresh_with_context(
        token.refresh_token, token.extra
    )
    oauth_tokens.upsert(
        session,
        provider=PROVIDER,
        account_key=token.account_key,
        bundle=bundle,
        extra_merge=token.extra,
    )
    session.commit()

    refreshed = oauth_tokens.get_decrypted(
        session, provider=PROVIDER, account_key=token.account_key
    )
    if refreshed is None:
        raise NotionTokenMissing("Notion token disappeared after refresh.")
    return refreshed
