"""Transparent proxy for the external kotx coding-agent API.

The frontend's "Runs" tab talks to kotx through here so the bearer token
stays server-side. We forward `/kotx/<path>` → `<KOTX_BASE_URL>/api/<path>`,
inject the `Authorization` header, and relay the upstream status, content
type, and body verbatim (kotx returns JSON for lists and `text/markdown`
for the TASK.md / REVIEW.md endpoints).

Only the documented subpaths are forwarded — this is a fixed integration
with one app, not an open proxy.
"""

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import get_settings

router = APIRouter(prefix="/kotx", tags=["kotx"])

_ALLOWED_PREFIXES = ("tasks", "containers", "runs")
_TIMEOUT = 30.0
# Hop-by-hop / framing headers we must not copy from the upstream response.
_SKIP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT"],
)
async def proxy(path: str, request: Request) -> Response:
    settings = get_settings()
    if not settings.kotx_base_url or not settings.kotx_api_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "kotx is not configured")

    if path.split("/", 1)[0] not in _ALLOWED_PREFIXES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown kotx path")

    url = f"{settings.kotx_base_url.rstrip('/')}/api/{path}"
    headers = {"Authorization": f"Bearer {settings.kotx_api_token}"}
    if ct := request.headers.get("content-type"):
        headers["content-type"] = ct
    body = await request.body()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        upstream = await client.request(
            request.method,
            url,
            params=request.query_params,
            content=body or None,
            headers=headers,
        )

    out_headers = {
        k: v for k, v in upstream.headers.items() if k.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=upstream.headers.get("content-type"),
    )
