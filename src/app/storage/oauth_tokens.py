"""Storage for OAuth tokens.

Tokens are stored as Fernet ciphertext (see `app.auth.crypto`). All decryption
happens through `get_decrypted` — never read the ciphertext columns directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.base import TokenBundle
from app.auth.crypto import decrypt_token, encrypt_token
from app.models.oauth_token import OAuthToken


@dataclass
class DecryptedToken:
    """Token row with plaintext credentials. Build only at point of use."""

    id: object
    provider: str
    account_key: str
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str]
    extra: dict

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        # 60s safety margin so a request mid-flight doesn't expire under us.
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(seconds=60)


def upsert(
    session: Session,
    *,
    provider: str,
    account_key: str,
    bundle: TokenBundle,
    extra_merge: dict | None = None,
) -> OAuthToken:
    """Insert or update the (provider, account_key) row with a new token bundle."""
    row = session.execute(
        select(OAuthToken).where(
            OAuthToken.provider == provider,
            OAuthToken.account_key == account_key,
        )
    ).scalar_one_or_none()

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=bundle.expires_in)
        if bundle.expires_in is not None
        else None
    )

    if row is None:
        row = OAuthToken(
            provider=provider,
            account_key=account_key,
            access_token_ct=encrypt_token(bundle.access_token),
            refresh_token_ct=(
                encrypt_token(bundle.refresh_token) if bundle.refresh_token else None
            ),
            expires_at=expires_at,
            scopes=list(bundle.scopes),
            extra=dict(bundle.extra) | (extra_merge or {}),
        )
        session.add(row)
    else:
        row.access_token_ct = encrypt_token(bundle.access_token)
        if bundle.refresh_token:
            row.refresh_token_ct = encrypt_token(bundle.refresh_token)
        row.expires_at = expires_at
        row.scopes = list(bundle.scopes)
        row.extra = {**(row.extra or {}), **bundle.extra, **(extra_merge or {})}
    session.flush()
    return row


def get_decrypted(
    session: Session, *, provider: str, account_key: str | None = None
) -> DecryptedToken | None:
    stmt = select(OAuthToken).where(OAuthToken.provider == provider)
    if account_key is not None:
        stmt = stmt.where(OAuthToken.account_key == account_key)
    stmt = stmt.order_by(OAuthToken.updated_at.desc()).limit(1)
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    return DecryptedToken(
        id=row.id,
        provider=row.provider,
        account_key=row.account_key,
        access_token=decrypt_token(row.access_token_ct),
        refresh_token=decrypt_token(row.refresh_token_ct) if row.refresh_token_ct else None,
        expires_at=row.expires_at,
        scopes=list(row.scopes or []),
        extra=dict(row.extra or {}),
    )


def list_account_keys(session: Session, *, provider: str) -> list[str]:
    """All account_keys connected for a provider, oldest first."""
    rows = session.execute(
        select(OAuthToken.account_key)
        .where(OAuthToken.provider == provider)
        .order_by(OAuthToken.created_at)
    ).all()
    return [r[0] for r in rows]


def set_extra(session: Session, *, provider: str, account_key: str, patch: dict) -> None:
    """Merge-patch the `extra` JSON for a token (e.g. to bump Gmail historyId)."""
    row = session.execute(
        select(OAuthToken).where(
            OAuthToken.provider == provider,
            OAuthToken.account_key == account_key,
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.extra = {**(row.extra or {}), **patch}
    session.flush()
