"""HTTP client for the kotx coding-agent API.

Used by the kotx ingestion source (transition polling + document fetches),
the webhook handler, and task close (discard). All functions degrade
gracefully when kotx is unconfigured: reads return empty/None, mutations
return False.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_TIMEOUT = 30.0

# /api/repos changes rarely; cache it in-process so the gmail policy check
# doesn't hit kotx on every notification email.
_REPOS_TTL_S = 600.0
_repos_cache: tuple[float, list[dict]] | None = None


def _base() -> tuple[str, dict[str, str]] | None:
    settings = get_settings()
    base_url = settings.kotx_base_url.strip().rstrip("/")
    token = settings.kotx_api_token.strip()
    if not base_url or not token:
        return None
    return base_url, {"Authorization": f"Bearer {token}"}


async def _get(path: str, params: dict | None = None) -> httpx.Response | None:
    cfg = _base()
    if cfg is None:
        return None
    base_url, headers = cfg
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
        return await client.get(f"{base_url}/api/{path}", params=params)


async def fetch_tasks(
    *, updated_since: datetime | None = None, scope: str = "active"
) -> list[dict]:
    params: dict[str, Any] = {"scope": scope}
    if updated_since is not None:
        params["updatedSince"] = updated_since.isoformat()
    resp = await _get("tasks", params)
    if resp is None:
        return []
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, list) else []


async def fetch_repos() -> list[dict]:
    global _repos_cache
    now = time.monotonic()
    if _repos_cache is not None and now - _repos_cache[0] < _REPOS_TTL_S:
        return _repos_cache[1]
    try:
        resp = await _get("repos")
        if resp is None:
            return []
        resp.raise_for_status()
        body = resp.json()
        repos = body if isinstance(body, list) else []
    except Exception:  # noqa: BLE001 — policy checks must not break ingestion
        log.exception("kotx fetch_repos failed")
        return _repos_cache[1] if _repos_cache is not None else []
    _repos_cache = (now, repos)
    return repos


async def tracked_repo_names() -> set[str]:
    """Lowercased full names of repos kotx tracks (for the gmail policy)."""
    return {
        str(r.get("fullName", "")).lower()
        for r in await fetch_repos()
        if r.get("fullName")
    }


async def fetch_doc(kotx_task_id: int, doc: str) -> str | None:
    """Fetch TASK.md (`doc='task'`) or REVIEW.md (`doc='review'`). None on 404."""
    resp = await _get(f"tasks/{kotx_task_id}/{doc}")
    if resp is None or resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


async def fetch_pr(kotx_task_id: int) -> dict | None:
    """The proposed PR `{title, body}`. None on 404."""
    resp = await _get(f"tasks/{kotx_task_id}/pr")
    if resp is None or resp.status_code == 404:
        return None
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, dict) else None


async def fetch_merge_context(kotx_task_id: int) -> dict | None:
    """The merge proposal's `{prNumber, approvedBy, reviewUrl, commentMarkdown}`.
    None on 404 (no merge proposal yet)."""
    resp = await _get(f"tasks/{kotx_task_id}/merge/context")
    if resp is None or resp.status_code == 404:
        return None
    resp.raise_for_status()
    body = resp.json()
    return body if isinstance(body, dict) else None


async def _post_action(kotx_task_id: int, verb: str) -> bool:
    """Best-effort `POST …/<verb>` lifecycle action (start/approve/merge/discard).
    False when unconfigured, not applicable in the current state (409), or gone
    (404) — the run's real state arrives on the next transition webhook."""
    cfg = _base()
    if cfg is None:
        return False
    base_url, headers = cfg
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
        resp = await client.post(f"{base_url}/api/tasks/{kotx_task_id}/{verb}")
    if resp.status_code in (404, 409):
        log.info("kotx %s skipped · task=%s status=%s", verb, kotx_task_id, resp.status_code)
        return False
    resp.raise_for_status()
    return True


async def start_task(kotx_task_id: int) -> bool:
    return await _post_action(kotx_task_id, "start")


async def approve_task(kotx_task_id: int) -> bool:
    return await _post_action(kotx_task_id, "approve")


async def merge_task(kotx_task_id: int) -> bool:
    return await _post_action(kotx_task_id, "merge")


async def comment_task(kotx_task_id: int) -> bool:
    return await _post_action(kotx_task_id, "comment")


async def discard_task(kotx_task_id: int) -> bool:
    return await _post_action(kotx_task_id, "discard")
