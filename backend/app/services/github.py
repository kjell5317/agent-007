"""Read-only GitHub access for the chat agent via the REST API.

Personal use: a single fine-grained PAT (read-only) in settings authenticates
GitHub's issue/PR search. Only search is wired — no write endpoints — so the
agent can read issues and pull requests but never mutate anything.

We hit the REST API with raw httpx rather than a client library, matching the
Gmail integration and keeping the surface to the one endpoint we need.
"""

from __future__ import annotations

import httpx

from app.config import get_settings

_API = "https://api.github.com"
_TIMEOUT = 10
_SEARCH_LIMIT = 15
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"

# `github_my_work`: the two queries a personal task agent actually cares about —
# what's assigned to me and what's waiting on my review.
_MY_WORK = (
    ("Assigned to you", "is:open assignee:@me"),
    ("Review requested from you", "is:open review-requested:@me"),
)


def is_connected() -> bool:
    return bool(get_settings().github_token.strip())


async def search_issues(query: str, *, limit: int = _SEARCH_LIMIT) -> str:
    items, total = await _fetch(query, limit)
    if not items:
        return f"No GitHub issues or PRs matched: {query}"
    listing = "\n".join(_format_item(it) for it in items)
    return f"{len(items)} of {total} results for `{query}`:\n{listing}"


async def my_work() -> str:
    sections: list[str] = []
    for label, query in _MY_WORK:
        items, total = await _fetch(query, 10)
        listing = "\n".join(_format_item(it) for it in items) if items else "(none)"
        sections.append(f"{label} ({total}):\n{listing}")
    return "\n\n".join(sections)


async def _fetch(query: str, limit: int) -> tuple[list[dict], int]:
    token = get_settings().github_token.strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
    }
    params = {"q": query, "per_page": limit, "advanced_search": "true"}
    async with httpx.AsyncClient(timeout=_TIMEOUT, base_url=_API) as client:
        resp = await client.get("/search/issues", params=params, headers=headers)
    if resp.status_code == 401:
        raise RuntimeError("GitHub token is invalid or expired; re-issue the PAT.")
    if resp.status_code == 403:
        raise RuntimeError(
            "GitHub denied the request — the token lacks Issues / Pull requests "
            "read access, or the search rate limit was hit."
        )
    resp.raise_for_status()
    data = resp.json()
    return list(data.get("items") or []), int(data.get("total_count") or 0)


def _repo_name(repository_url: str) -> str:
    marker = "/repos/"
    idx = repository_url.find(marker)
    return repository_url[idx + len(marker) :] if idx >= 0 else repository_url


def _format_item(item: dict) -> str:
    repo = _repo_name(str(item.get("repository_url") or ""))
    number = item.get("number")
    is_pr = "pull_request" in item
    state = str(item.get("state") or "")
    if is_pr:
        pr = item.get("pull_request") or {}
        if pr.get("merged_at"):
            state = "merged"
        elif item.get("draft"):
            state = f"draft/{state}"
    kind = "PR" if is_pr else "issue"
    author = (item.get("user") or {}).get("login", "?")
    updated = str(item.get("updated_at") or "")[:10]
    title = item.get("title") or "(untitled)"
    head = f"[{repo}#{number}] {title} ({kind}, {state}, by {author}"
    if updated:
        head += f", updated {updated}"
    head += f") — {item.get('html_url') or ''}"
    body = " ".join(str(item.get("body") or "").split())
    if body:
        head += f"\n  {body[:160]}"
    return head
