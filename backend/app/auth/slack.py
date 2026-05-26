"""Slack OAuth 2.0 provider — user-token flow.

Slack apps are workspace-scoped, so two workspaces means two apps and two
sets of (client_id, client_secret). The provider takes an `app_name` selecting
which app's credentials to use. The app name is carried through the OAuth
state token so the callback knows which set to exchange against.

Token storage is unaffected — `account_key = team_id:user_id` already
differentiates workspaces regardless of which app issued the token.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.auth.base import OAuthProvider, TokenBundle, register_provider
from app.config import get_settings

_AUTH_URL = "https://slack.com/oauth/v2/authorize"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_AUTH_TEST_URL = "https://slack.com/api/auth.test"

# Least-privilege user scopes. Slack splits listing from reading: `*:read`
# scopes authorize `users.conversations` (listing the channels the user is in);
# `*:history` scopes authorize `conversations.history` (reading messages from
# them). We need both pairs.
DEFAULT_USER_SCOPES = [
    "channels:read", "channels:history",
    "groups:read", "groups:history",
    "im:read", "im:history",
    "mpim:read", "mpim:history",
    "users:read",
]


@register_provider("slack")
class SlackOAuthProvider(OAuthProvider):
    def __init__(self, app_name: str | None = None):
        self.app_name = app_name

    def _creds(self) -> tuple[str, str, str]:
        """(app_name, client_id, client_secret) for the selected app.

        If no app_name was given and only one app is configured, pick that one.
        Otherwise raise so the caller knows to disambiguate via `?app=`.
        """
        apps = get_settings().slack_apps
        if not apps:
            raise RuntimeError(
                "No Slack apps configured. Set SLACK_APPS in .env to a JSON object: "
                '{"name":{"client_id":"...","client_secret":"..."}}'
            )
        if self.app_name is None:
            if len(apps) == 1:
                name = next(iter(apps))
            else:
                raise RuntimeError(
                    f"Multiple Slack apps configured ({list(apps)}). "
                    "Specify ?app=<name> on /oauth/slack/authorize."
                )
        else:
            name = self.app_name
        if name not in apps:
            raise RuntimeError(f"Unknown Slack app {name!r}. Configured: {list(apps)}.")
        creds = apps[name]
        return name, creds["client_id"], creds["client_secret"]

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        _, client_id, _ = self._creds()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "user_scope": ",".join(DEFAULT_USER_SCOPES),
        }
        return f"{_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        name, client_id, client_secret = self._creds()
        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_TOKEN_URL, data=data)
            resp.raise_for_status()
            payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"slack oauth exchange failed: {payload.get('error')}")
        bundle = _bundle_from_token_response(payload)
        bundle.extra["app_name"] = name
        return bundle

    async def refresh(self, refresh_token: str) -> TokenBundle:
        raise NotImplementedError(
            "Slack token rotation is not enabled for this app — tokens do not expire."
        )

    async def identify(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _AUTH_TEST_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"slack auth.test failed: {payload.get('error')}")
        return f"{payload['team_id']}:{payload['user_id']}"


def _bundle_from_token_response(payload: dict) -> TokenBundle:
    """The v2.access response nests the user token under `authed_user`."""
    user = payload.get("authed_user") or {}
    access_token = user.get("access_token")
    if not access_token:
        raise RuntimeError(
            "slack oauth response missing authed_user.access_token — was user_scope set?"
        )
    scope = user.get("scope", "")
    extra = {
        "team": payload.get("team"),
        "team_id": (payload.get("team") or {}).get("id"),
        "authed_user_id": user.get("id"),
        "bot_user_id": payload.get("bot_user_id"),
        "app_id": payload.get("app_id"),
    }
    return TokenBundle(
        access_token=access_token,
        refresh_token=None,
        expires_in=None,
        scopes=[s for s in scope.split(",") if s],
        extra=extra,
    )
