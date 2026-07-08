import os
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import app.auth.notion_mcp as notion_mcp  # noqa: E402
from app.auth.base import OAuthAuthorization, TokenBundle  # noqa: E402
from app.api import oauth as oauth_api  # noqa: E402
from app.config.settings import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _response(
    status_code: int,
    *,
    json: dict | None = None,
    text: str | None = None,
    headers: dict | None = None,
    url: str = "https://example.test",
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=json,
        text=text,
        headers=headers,
        request=httpx.Request("GET", url),
    )


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict | None, dict | None]] = []
        self.get_responses: list[httpx.Response] = []
        self.post_responses: list[httpx.Response] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, url: str):
        self.gets.append(url)
        return self.get_responses.pop(0)

    async def post(self, url: str, *, json: dict | None = None, data: dict | None = None):
        self.posts.append((url, json, data))
        return self.post_responses.pop(0)


@pytest.mark.asyncio
async def test_protected_resource_discovery_uses_www_authenticate_hint():
    client = FakeAsyncClient()
    client.get_responses = [
        _response(404, text="missing"),
        _response(
            401,
            headers={
                "www-authenticate": (
                    'Bearer resource_metadata="https://mcp.notion.com/meta"'
                )
            },
        ),
        _response(
            200,
            json={
                "resource": "https://mcp.notion.com/mcp",
                "authorization_servers": ["https://mcp.notion.com"],
            },
        ),
    ]

    metadata_url, metadata = await notion_mcp.discover_protected_resource_metadata(
        client, "https://mcp.notion.com/mcp"
    )

    assert metadata_url == "https://mcp.notion.com/meta"
    assert metadata["authorization_servers"] == ["https://mcp.notion.com"]
    assert client.gets == [
        "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp",
        "https://mcp.notion.com/mcp",
        "https://mcp.notion.com/meta",
    ]


@pytest.mark.asyncio
async def test_authorize_registers_dynamic_client_and_uses_pkce(monkeypatch):
    monkeypatch.setenv("MCP_NOTION_URL", "https://mcp.notion.com/mcp")
    monkeypatch.setenv("MCP_OAUTH_CLIENT_NAME", "Agent 007")
    get_settings.cache_clear()
    fake_client = FakeAsyncClient()
    fake_client.get_responses = [
        _response(
            200,
            json={
                "resource": "https://mcp.notion.com/mcp",
                "authorization_servers": ["https://mcp.notion.com"],
            },
        ),
        _response(
            200,
            json={
                "issuer": "https://mcp.notion.com",
                "authorization_endpoint": "https://mcp.notion.com/authorize",
                "token_endpoint": "https://mcp.notion.com/token",
                "registration_endpoint": "https://mcp.notion.com/register",
            },
        ),
    ]
    fake_client.post_responses = [
        _response(
            201,
            json={
                "client_id": "client-123",
                "client_secret": "secret-123",
                "redirect_uris": ["http://localhost/callback"],
            },
        )
    ]
    monkeypatch.setattr(notion_mcp.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr(
        notion_mcp,
        "generate_pkce",
        lambda: notion_mcp.PkcePair(verifier="verifier-123", challenge="challenge-123"),
    )

    auth = await notion_mcp.NotionMcpOAuthProvider().authorize(
        state="state-123", redirect_uri="http://localhost/callback"
    )

    params = notion_mcp.query_params(auth.url)
    assert auth.url.startswith("https://mcp.notion.com/authorize?")
    assert params["client_id"] == "client-123"
    assert params["code_challenge"] == "challenge-123"
    assert params["code_challenge_method"] == "S256"
    assert params["resource"] == "https://mcp.notion.com/mcp"
    assert auth.context == {
        "code_verifier": "verifier-123",
        "client_id": "client-123",
        "client_secret": "secret-123",
        "token_endpoint": "https://mcp.notion.com/token",
        "issuer": "https://mcp.notion.com",
        "mcp_server_url": "https://mcp.notion.com/mcp",
        "resource": "https://mcp.notion.com/mcp",
        "resource_metadata_url": (
            "https://mcp.notion.com/.well-known/oauth-protected-resource/mcp"
        ),
        "authorization_server_metadata_url": (
            "https://mcp.notion.com/.well-known/oauth-authorization-server"
        ),
        "registration": {
            "client_id": "client-123",
            "client_secret": "secret-123",
            "redirect_uris": ["http://localhost/callback"],
        },
    }
    assert fake_client.posts[0] == (
        "https://mcp.notion.com/register",
        {
            "client_name": "Agent 007",
            "redirect_uris": ["http://localhost/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        None,
    )


@pytest.mark.asyncio
async def test_code_exchange_posts_code_verifier_and_stores_metadata(monkeypatch):
    fake_client = FakeAsyncClient()
    fake_client.post_responses = [
        _response(
            200,
            json={
                "access_token": "access-123",
                "refresh_token": "refresh-123",
                "expires_in": 3600,
                "scope": "read write",
                "token_type": "Bearer",
            },
        )
    ]
    monkeypatch.setattr(notion_mcp.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    context = {
        "code_verifier": "verifier-123",
        "client_id": "client-123",
        "client_secret": "secret-123",
        "token_endpoint": "https://mcp.notion.com/token",
        "issuer": "https://mcp.notion.com",
        "mcp_server_url": "https://mcp.notion.com/mcp",
        "resource": "https://mcp.notion.com/mcp",
        "resource_metadata_url": "https://mcp.notion.com/resource-meta",
        "authorization_server_metadata_url": "https://mcp.notion.com/as-meta",
        "registration": {"client_id": "client-123"},
    }

    bundle = await notion_mcp.NotionMcpOAuthProvider().exchange_code_with_context(
        code="code-123", redirect_uri="http://localhost/callback", context=context
    )

    assert fake_client.posts[0] == (
        "https://mcp.notion.com/token",
        None,
        {
            "grant_type": "authorization_code",
            "code": "code-123",
            "redirect_uri": "http://localhost/callback",
            "client_id": "client-123",
            "code_verifier": "verifier-123",
            "resource": "https://mcp.notion.com/mcp",
            "client_secret": "secret-123",
        },
    )
    assert bundle.access_token == "access-123"
    assert bundle.refresh_token == "refresh-123"
    assert bundle.scopes == ["read", "write"]
    assert bundle.extra["client_id"] == "client-123"
    assert bundle.extra["client_secret"] == "secret-123"
    assert bundle.extra["token_endpoint"] == "https://mcp.notion.com/token"


@pytest.mark.asyncio
async def test_refresh_preserves_or_updates_refresh_token(monkeypatch):
    fake_client = FakeAsyncClient()
    fake_client.post_responses = [
        _response(200, json={"access_token": "access-1", "expires_in": 3600}),
        _response(
            200,
            json={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
            },
        ),
    ]
    monkeypatch.setattr(notion_mcp.httpx, "AsyncClient", lambda *a, **kw: fake_client)
    context = {
        "client_id": "client-123",
        "token_endpoint": "https://mcp.notion.com/token",
        "mcp_server_url": "https://mcp.notion.com/mcp",
        "resource": "https://mcp.notion.com/mcp",
        "resource_metadata_url": "https://mcp.notion.com/resource-meta",
        "authorization_server_metadata_url": "https://mcp.notion.com/as-meta",
        "registration": {"client_id": "client-123"},
    }
    provider = notion_mcp.NotionMcpOAuthProvider()

    preserved = await provider.refresh_with_context("refresh-1", context)
    rotated = await provider.refresh_with_context("refresh-1", context)

    assert preserved.refresh_token == "refresh-1"
    assert rotated.refresh_token == "refresh-2"
    assert fake_client.posts[0][2]["grant_type"] == "refresh_token"
    assert fake_client.posts[0][2]["refresh_token"] == "refresh-1"


@pytest.mark.asyncio
async def test_oauth_callback_validates_missing_and_expired_state():
    oauth_api._state_store.clear()

    with pytest.raises(HTTPException) as missing_state:
        await oauth_api.callback("notion", code="code-123", state=None, session=object())
    assert missing_state.value.status_code == 400
    assert missing_state.value.detail == "Missing OAuth state"

    with pytest.raises(HTTPException) as missing_code:
        await oauth_api.callback("notion", code=None, state="state-123", session=object())
    assert missing_code.value.status_code == 400
    assert missing_code.value.detail == "Missing OAuth code"

    oauth_api._state_store["expired"] = oauth_api._State(
        "notion", None, time.time() - 1, {"code_verifier": "old"}
    )
    with pytest.raises(HTTPException) as expired:
        await oauth_api.callback("notion", code="code-123", state="expired", session=object())
    assert expired.value.status_code == 400
    assert expired.value.detail == "Invalid or expired state"


@pytest.mark.asyncio
async def test_oauth_callback_passes_transient_context_and_stores_bundle(monkeypatch):
    oauth_api._state_store.clear()
    context = {"code_verifier": "verifier-123", "mcp_server_url": "https://mcp.notion.com/mcp"}
    oauth_api._state_store["valid"] = oauth_api._State(
        "notion", None, time.time() + 60, context
    )
    captured = {}

    class FakeProvider:
        async def exchange_code_with_context(self, code, redirect_uri, context):
            captured["exchange"] = (code, redirect_uri, context)
            return TokenBundle("access-123", "refresh-123", 3600, ["read"], {"x": "y"})

        async def identify_with_context(self, access_token, context):
            captured["identify"] = (access_token, context)
            return "notion-mcp:https://mcp.notion.com/mcp"

    def fake_upsert(session, *, provider, account_key, bundle):
        captured["upsert"] = (session, provider, account_key, bundle)

    session = SimpleNamespace(commit=lambda: captured.setdefault("committed", True))
    monkeypatch.setattr(oauth_api, "_build_provider", lambda provider, app: FakeProvider())
    monkeypatch.setattr(oauth_api.oauth_tokens, "upsert", fake_upsert)
    monkeypatch.setattr(
        oauth_api,
        "get_settings",
        lambda: SimpleNamespace(notion_oauth_redirect_uri="http://localhost/callback"),
    )

    result = await oauth_api.callback(
        "notion", code="code-123", state="valid", session=session
    )

    assert captured["exchange"] == ("code-123", "http://localhost/callback", context)
    assert captured["identify"] == ("access-123", context)
    assert captured["upsert"][1:3] == (
        "notion",
        "notion-mcp:https://mcp.notion.com/mcp",
    )
    assert captured["committed"] is True
    assert result == {
        "provider": "notion",
        "app": None,
        "account_key": "notion-mcp:https://mcp.notion.com/mcp",
        "scopes": ["read"],
    }


@pytest.mark.asyncio
async def test_oauth_authorize_returns_400_and_drops_state_on_registration_failure(monkeypatch):
    oauth_api._state_store.clear()

    class FailingProvider:
        async def authorize(self, state, redirect_uri):
            raise RuntimeError("notion mcp oauth registration failed: no registration endpoint")

    monkeypatch.setattr(oauth_api, "_build_provider", lambda provider, app: FailingProvider())
    monkeypatch.setattr(
        oauth_api,
        "get_settings",
        lambda: SimpleNamespace(notion_oauth_redirect_uri="http://localhost/callback"),
    )

    with pytest.raises(HTTPException) as exc:
        await oauth_api.authorize("notion", app=None)

    assert exc.value.status_code == 400
    assert exc.value.detail == "notion mcp oauth registration failed: no registration endpoint"
    assert oauth_api._state_store == {}


@pytest.mark.asyncio
async def test_oauth_authorize_saves_provider_context(monkeypatch):
    oauth_api._state_store.clear()

    class ContextProvider:
        async def authorize(self, state, redirect_uri):
            return OAuthAuthorization(
                url="https://mcp.notion.com/authorize",
                context={"code_verifier": "verifier-123"},
            )

    monkeypatch.setattr(oauth_api, "_build_provider", lambda provider, app: ContextProvider())
    monkeypatch.setattr(
        oauth_api,
        "get_settings",
        lambda: SimpleNamespace(notion_oauth_redirect_uri="http://localhost/callback"),
    )

    response = await oauth_api.authorize("notion", app=None)

    assert response.headers["location"] == "https://mcp.notion.com/authorize"
    assert len(oauth_api._state_store) == 1
    issued = next(iter(oauth_api._state_store.values()))
    assert issued.context == {"code_verifier": "verifier-123"}
