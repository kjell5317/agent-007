"""Notion MCP OAuth provider.

Notion's hosted MCP server follows the MCP OAuth pattern: discover protected
resource metadata, discover authorization-server metadata, dynamically register
a client, and use PKCE for the authorization-code flow.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

import httpx

from app.auth.base import OAuthAuthorization, OAuthProvider, TokenBundle, register_provider
from app.config import get_settings

_TIMEOUT = 10


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str


def generate_pkce() -> PkcePair:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PkcePair(verifier=verifier, challenge=challenge)


def _json_or_raise(resp: httpx.Response, action: str) -> dict[str, Any]:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300]
        raise RuntimeError(
            f"notion mcp oauth {action} failed: {exc.response.status_code} {detail}"
        ) from exc
    try:
        payload = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"notion mcp oauth {action} failed: response was not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"notion mcp oauth {action} failed: response was not an object")
    return payload


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return parsed._replace(path=path, params="", query="", fragment="").geturl()


def protected_resource_metadata_url(mcp_url: str) -> str:
    parsed = urlparse(mcp_url)
    path = parsed.path.rstrip("/")
    well_known_path = "/.well-known/oauth-protected-resource"
    if path:
        well_known_path = f"{well_known_path}{path}"
    return parsed._replace(path=well_known_path, params="", query="", fragment="").geturl()


def authorization_server_metadata_url(issuer: str) -> str:
    return urljoin(issuer.rstrip("/") + "/", ".well-known/oauth-authorization-server")


def _resource_metadata_from_www_authenticate(header: str) -> str | None:
    match = re.search(r'(?:^|[\s,])resource_metadata="([^"]+)"', header, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?:^|[\s,])resource_metadata=([^\s,]+)", header, re.I)
    return match.group(1) if match else None


async def _get(client: httpx.AsyncClient, url: str, action: str) -> httpx.Response:
    try:
        return await client.get(url)
    except httpx.RequestError as exc:
        raise RuntimeError(f"notion mcp oauth {action} failed: {exc}") from exc


async def _post_form(
    client: httpx.AsyncClient, url: str, data: dict[str, Any], action: str
) -> httpx.Response:
    try:
        return await client.post(url, data=data)
    except httpx.RequestError as exc:
        raise RuntimeError(f"notion mcp oauth {action} failed: {exc}") from exc


async def _post_json(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], action: str
) -> httpx.Response:
    try:
        return await client.post(url, json=payload)
    except httpx.RequestError as exc:
        raise RuntimeError(f"notion mcp oauth {action} failed: {exc}") from exc


async def discover_protected_resource_metadata(
    client: httpx.AsyncClient, mcp_url: str
) -> tuple[str, dict[str, Any]]:
    metadata_url = protected_resource_metadata_url(mcp_url)
    resp = await _get(client, metadata_url, "protected-resource metadata discovery")
    if resp.is_success:
        return metadata_url, _json_or_raise(resp, "protected-resource metadata discovery")

    probe = await _get(client, mcp_url, "protected-resource metadata probe")
    hinted = _resource_metadata_from_www_authenticate(
        probe.headers.get("www-authenticate", "")
    )
    if not hinted:
        _json_or_raise(resp, "protected-resource metadata discovery")
    hinted_resp = await _get(client, hinted, "protected-resource metadata discovery")
    return hinted, _json_or_raise(hinted_resp, "protected-resource metadata discovery")


async def discover_authorization_server_metadata(
    client: httpx.AsyncClient, protected_metadata: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    servers = protected_metadata.get("authorization_servers") or []
    if not servers:
        raise RuntimeError("notion mcp oauth discovery failed: no authorization server")
    issuer = str(servers[0])
    metadata_url = authorization_server_metadata_url(issuer)
    resp = await _get(client, metadata_url, "authorization-server metadata discovery")
    metadata = _json_or_raise(resp, "authorization-server metadata discovery")
    return metadata_url, metadata


def _registration_payload(redirect_uri: str) -> dict[str, Any]:
    settings = get_settings()
    payload: dict[str, Any] = {
        "client_name": settings.mcp_oauth_client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if settings.mcp_oauth_client_uri:
        payload["client_uri"] = settings.mcp_oauth_client_uri
    if settings.mcp_oauth_logo_uri:
        payload["logo_uri"] = settings.mcp_oauth_logo_uri
    return payload


async def dynamically_register_client(
    client: httpx.AsyncClient, as_metadata: dict[str, Any], redirect_uri: str
) -> dict[str, Any]:
    endpoint = as_metadata.get("registration_endpoint")
    if not endpoint:
        raise RuntimeError("notion mcp oauth registration failed: no registration endpoint")
    resp = await _post_json(
        client, str(endpoint), _registration_payload(redirect_uri), "client registration"
    )
    registration = _json_or_raise(resp, "client registration")
    if not registration.get("client_id"):
        raise RuntimeError("notion mcp oauth registration failed: missing client_id")
    return registration


def build_authorization_url(
    *,
    as_metadata: dict[str, Any],
    registration: dict[str, Any],
    redirect_uri: str,
    state: str,
    pkce: PkcePair,
    resource: str,
) -> str:
    endpoint = as_metadata.get("authorization_endpoint")
    if not endpoint:
        raise RuntimeError("notion mcp oauth authorization failed: no authorization endpoint")
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": str(registration["client_id"]),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "resource": resource,
    }
    return f"{endpoint}?{urlencode(params)}"


def _token_bundle_from_response(
    payload: dict[str, Any],
    *,
    context: dict[str, Any],
    refresh_token_fallback: str | None = None,
) -> TokenBundle:
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("notion mcp oauth token exchange failed: missing access_token")
    refresh_token = payload.get("refresh_token") or refresh_token_fallback
    raw_scope = payload.get("scope")
    if isinstance(raw_scope, str):
        scopes = raw_scope.split()
    elif isinstance(raw_scope, list):
        scopes = [str(s) for s in raw_scope]
    else:
        scopes = []
    extra = {
        k: v
        for k, v in payload.items()
        if k not in {"access_token", "refresh_token"}
    }
    extra.update(
        {
            "mcp_server_url": context["mcp_server_url"],
            "resource": context["resource"],
            "resource_metadata_url": context["resource_metadata_url"],
            "authorization_server_metadata_url": context["authorization_server_metadata_url"],
            "token_endpoint": context.get("token_endpoint"),
            "issuer": context.get("issuer"),
            "client_id": context["client_id"],
            "registration": context.get("registration", {}),
        }
    )
    if context.get("client_secret"):
        extra["client_secret"] = context["client_secret"]
    return TokenBundle(
        access_token=str(access_token),
        refresh_token=str(refresh_token) if refresh_token else None,
        expires_in=payload.get("expires_in"),
        scopes=scopes,
        extra=extra,
    )


@register_provider("notion")
class NotionMcpOAuthProvider(OAuthProvider):
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        raise RuntimeError("Notion MCP OAuth requires async metadata discovery")

    async def authorize(self, state: str, redirect_uri: str) -> OAuthAuthorization:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resource_metadata_url, protected_metadata = await discover_protected_resource_metadata(
                client, settings.mcp_notion_url
            )
            as_metadata_url, as_metadata = await discover_authorization_server_metadata(
                client, protected_metadata
            )
            registration = await dynamically_register_client(client, as_metadata, redirect_uri)

        pkce = generate_pkce()
        resource = str(protected_metadata.get("resource") or _normalize_url(settings.mcp_notion_url))
        url = build_authorization_url(
            as_metadata=as_metadata,
            registration=registration,
            redirect_uri=redirect_uri,
            state=state,
            pkce=pkce,
            resource=resource,
        )
        context = {
            "code_verifier": pkce.verifier,
            "client_id": registration["client_id"],
            "client_secret": registration.get("client_secret"),
            "token_endpoint": as_metadata.get("token_endpoint"),
            "issuer": as_metadata.get("issuer") or (protected_metadata.get("authorization_servers") or [None])[0],
            "mcp_server_url": _normalize_url(settings.mcp_notion_url),
            "resource": resource,
            "resource_metadata_url": resource_metadata_url,
            "authorization_server_metadata_url": as_metadata_url,
            "registration": registration,
        }
        return OAuthAuthorization(url=url, context=context)

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        raise RuntimeError("Notion MCP OAuth callback missing authorization context")

    async def exchange_code_with_context(
        self, code: str, redirect_uri: str, context: dict | None
    ) -> TokenBundle:
        if not context:
            raise RuntimeError("Notion MCP OAuth callback missing authorization context")
        token_endpoint = context.get("token_endpoint")
        if not token_endpoint:
            raise RuntimeError("Notion MCP OAuth callback missing token endpoint")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": context["client_id"],
            "code_verifier": context["code_verifier"],
            "resource": context["resource"],
        }
        if context.get("client_secret"):
            data["client_secret"] = context["client_secret"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await _post_form(client, str(token_endpoint), data, "token exchange")
            payload = _json_or_raise(resp, "token exchange")
        return _token_bundle_from_response(payload, context=context)

    async def refresh(self, refresh_token: str) -> TokenBundle:
        raise RuntimeError("Notion MCP OAuth refresh requires stored provider metadata")

    async def refresh_with_context(
        self, refresh_token: str, context: dict | None
    ) -> TokenBundle:
        if not context:
            raise RuntimeError("Notion MCP OAuth refresh missing provider metadata")
        token_endpoint = context.get("token_endpoint") or context.get("token_endpoint_url")
        if not token_endpoint:
            raise RuntimeError("Notion MCP OAuth refresh missing token endpoint")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": context["client_id"],
            "resource": context["resource"],
        }
        if context.get("client_secret"):
            data["client_secret"] = context["client_secret"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await _post_form(client, str(token_endpoint), data, "token refresh")
            payload = _json_or_raise(resp, "token refresh")
        return _token_bundle_from_response(
            payload, context=context, refresh_token_fallback=refresh_token
        )

    async def identify(self, access_token: str) -> str:
        settings = get_settings()
        return f"notion-mcp:{_normalize_url(settings.mcp_notion_url)}"

    async def identify_with_context(self, access_token: str, context: dict | None) -> str:
        if context and context.get("mcp_server_url"):
            return f"notion-mcp:{context['mcp_server_url']}"
        return await self.identify(access_token)


def query_params(url: str) -> dict[str, str]:
    """Test helper for checking generated authorization URLs."""
    return dict(parse_qsl(urlparse(url).query))
