"""Google OAuth 2.0 provider.

Implements the generic `OAuthProvider` contract against Google's endpoints.
Used by every Google-backed integration (Gmail, Calendar, health/sleep) — the
same access token can be reused across Google APIs as long as the requested
scopes cover them.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.auth.base import OAuthProvider, TokenBundle, register_provider
from app.config import get_settings

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Least-privilege default. Gmail read-only covers the ingestion path;
# calendar.events covers the Calendar service; openid+email drive SSO. Existing
# users must re-authorize once after this list changes — Google's consent screen
# handles that because we pass prompt=consent below.
DEFAULT_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    # Drive read-only powers the chat/search federated Drive lookup.
    "https://www.googleapis.com/auth/drive.readonly",
]

# The Google Health API refuses any token that also carries non-health scopes,
# so sleep lives on a SEPARATE grant (same OAuth client) requesting only these.
# openid+email stay in — they're identity scopes, not on the disallowed list —
# so identify() still resolves the account email.
HEALTH_SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]


@register_provider("google")
class GoogleOAuthProvider(OAuthProvider):
    scopes = DEFAULT_SCOPES
    # Union past grants so incremental SSO re-auth keeps Gmail/Calendar access.
    include_granted_scopes = True

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        s = get_settings()
        params = {
            "client_id": s.google_oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            # offline + consent ensures we always get a refresh_token, even on re-auth
            "access_type": "offline",
            "prompt": "consent",
        }
        if self.include_granted_scopes:
            params["include_granted_scopes"] = "true"
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


@register_provider("google_health")
class GoogleHealthOAuthProvider(GoogleOAuthProvider):
    """Same OAuth client as `google`, but a health-only grant.

    Its token must not carry Gmail/Calendar scopes (the Health API rejects
    those), so it requests `HEALTH_SCOPES` alone and does NOT union previously
    granted scopes.
    """

    scopes = HEALTH_SCOPES
    include_granted_scopes = False


def _bundle_from_token_response(payload: dict) -> TokenBundle:
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token"),
        expires_in=payload.get("expires_in"),
        scopes=payload.get("scope", "").split() if payload.get("scope") else [],
        extra={k: v for k, v in payload.items() if k not in {"access_token", "refresh_token"}},
    )
