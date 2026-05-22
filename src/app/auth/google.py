"""Google OAuth 2.0 provider.

Implements the generic `OAuthProvider` contract against Google's endpoints.
Used by every Google-backed source (Gmail today; Calendar / Drive later if we
want them) — the same access token can be reused across Google APIs as long
as the requested scopes cover them.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.auth.base import OAuthProvider, TokenBundle, register_provider
from app.config import get_settings

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Least-privilege default. Gmail read-only is enough for the ingestion path;
# add more (`gmail.modify`, `calendar.readonly`, ...) when a source needs them.
DEFAULT_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
]


@register_provider("google")
class GoogleOAuthProvider(OAuthProvider):
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        s = get_settings()
        params = {
            "client_id": s.google_oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(DEFAULT_SCOPES),
            "state": state,
            # offline + consent ensures we always get a refresh_token, even on re-auth
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        return f"{_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        s = get_settings()
        data = {
            "client_id": s.google_oauth_client_id,
            "client_secret": s.google_oauth_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_TOKEN_URL, data=data)
            resp.raise_for_status()
            payload = resp.json()
        return _bundle_from_token_response(payload)

    async def refresh(self, refresh_token: str) -> TokenBundle:
        s = get_settings()
        data = {
            "client_id": s.google_oauth_client_id,
            "client_secret": s.google_oauth_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_TOKEN_URL, data=data)
            resp.raise_for_status()
            payload = resp.json()
        # Google's refresh response often omits refresh_token; preserve the old one.
        payload.setdefault("refresh_token", refresh_token)
        return _bundle_from_token_response(payload)

    async def identify(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            return resp.json()["email"]


def _bundle_from_token_response(payload: dict) -> TokenBundle:
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        scopes=payload.get("scope", "").split() if payload.get("scope") else [],
        extra={k: v for k, v in payload.items() if k not in {"access_token", "refresh_token"}},
    )
