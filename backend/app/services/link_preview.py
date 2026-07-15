"""Server-side link unfurl for the chat UI (WhatsApp-style previews).

Fetches a URL and pulls Open Graph / basic metadata (title, description, site
name, image) out of the HTML. Runs server-side so the browser isn't blocked by
CORS, and guarded against SSRF: only public http(s) hosts (re-checked on every
redirect hop), a capped body, and a short timeout. Results (including misses)
are cached in-process for an hour so re-renders don't refetch.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 6.0
_MAX_BYTES = 512_000
_MAX_REDIRECTS = 4
_CACHE_TTL = 60 * 60  # 1 hour
_CACHE_MAX = 512
_UA = "Mozilla/5.0 (compatible; TaskAgentLinkPreview/1.0)"

# url -> (expires_at_monotonic, preview | None)
_cache: dict[str, tuple[float, dict | None]] = {}


class LinkPreviewError(Exception):
    pass


async def get_link_preview(url: str) -> dict | None:
    """Preview dict for `url`, or None when it can't be previewed (unreachable,
    non-HTML, no title, or blocked as non-public). Never raises."""
    url = (url or "").strip()
    if not url:
        return None
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and hit[0] > now:
        return hit[1]

    result: dict | None
    try:
        final_url, text = await _fetch_html(url)
        data = _extract(text, final_url)
        result = {"url": url, **data} if data.get("title") else None
    except (httpx.HTTPError, LinkPreviewError, ValueError) as exc:
        log.info("link preview failed · %s · %s: %s", url, type(exc).__name__, exc)
        result = None

    _prune(now)
    _cache[url] = (now + _CACHE_TTL, result)
    return result


async def _fetch_html(url: str) -> tuple[str, str]:
    """(final_url, html) following up to _MAX_REDIRECTS hops, re-checking the
    host is public at each one. Reads at most _MAX_BYTES of an HTML body."""
    headers = {"user-agent": _UA, "accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=_TIMEOUT, headers=headers
    ) as client:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise LinkPreviewError("unsupported url")
            await _ensure_public(parsed.hostname)

            resp = await client.send(client.build_request("GET", current), stream=True)
            try:
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype and "xml" not in ctype:
                    raise LinkPreviewError(f"not html ({ctype!r})")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= _MAX_BYTES:
                        break
                text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                return str(resp.url), text
            finally:
                await resp.aclose()
        raise LinkPreviewError("too many redirects")


async def _ensure_public(host: str) -> None:
    """Raise unless every address `host` resolves to is a public IP — blocks
    SSRF into the loopback/private/link-local/reserved ranges."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise LinkPreviewError("dns resolution failed") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise LinkPreviewError(f"non-public host: {ip}")


def _extract(html_text: str, base_url: str) -> dict:
    title = (
        _meta(html_text, "og:title")
        or _meta(html_text, "twitter:title")
        or _title_tag(html_text)
    )
    description = (
        _meta(html_text, "og:description")
        or _meta(html_text, "twitter:description")
        or _meta(html_text, "description")
    )
    image = _meta(html_text, "og:image") or _meta(html_text, "twitter:image")
    return {
        "title": title,
        "description": description,
        "site_name": _meta(html_text, "og:site_name"),
        "image": urljoin(base_url, image) if image else None,
    }


def _meta(html_text: str, prop: str) -> str | None:
    """Content of a <meta property=…> / <meta name=…> tag, tolerating either
    attribute order and single/double quotes."""
    esc = re.escape(prop)
    for pat in (
        rf'<meta[^>]+(?:property|name)=["\']{esc}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{esc}["\']',
    ):
        m = re.search(pat, html_text, re.IGNORECASE)
        if m and m.group(1).strip():
            return html.unescape(m.group(1)).strip()
    return None


def _title_tag(html_text: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    return html.unescape(m.group(1)).strip() if m and m.group(1).strip() else None


def _prune(now: float) -> None:
    if len(_cache) < _CACHE_MAX:
        return
    for k in [k for k, (exp, _) in _cache.items() if exp <= now]:
        _cache.pop(k, None)
    if len(_cache) >= _CACHE_MAX:  # still full of live entries — reset (low-traffic).
        _cache.clear()
