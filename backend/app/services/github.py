"""Read-only GitHub access for the chat agent via the REST API.

Personal use: a single fine-grained PAT (read-only) in settings authenticates
GitHub's issue/PR search. Only search is wired — no write endpoints — so the
agent can read issues and pull requests but never mutate anything.

`github_search` is scoped to repositories the user contributes to (owns, is a
collaborator on, or is an org member of — from `GET /user/repos`), so free-text
queries never surface unrelated public-repo noise. `github_my_work` is already
self-scoped (assigned to / review requested from the user) and stays global.

We hit the REST API with raw httpx rather than a client library, matching the
Gmail integration and keeping the surface to the few endpoints we need.
"""

from __future__ import annotations

import re
import time

import httpx

from app.config import get_settings

_API = "https://api.github.com"
_TIMEOUT = 10
_SEARCH_LIMIT = 15
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"

# Contributing repos rarely change within a session; cache the lookup in-process.
_REPOS_TTL = 600.0
_REPO_PAGES = 5  # up to 500 repos
# Above this many distinct owners, appending owner qualifiers would blow the
# ~256-char search-query limit, so fall back to post-filtering only.
_MAX_OWNER_QUALIFIERS = 12
# A query that already names a repo/user/org is left alone (only post-filtered),
# so we don't broaden an explicit scope the caller chose.
_SCOPED_RE = re.compile(r"\b(repo|user|org):", re.I)

# `github_my_work`: the two queries a personal task agent actually cares about —
# what's assigned to me and what's waiting on my review.
_MY_WORK = (
    ("Assigned to you", "is:open assignee:@me"),
    ("Review requested from you", "is:open review-requested:@me"),
)

_repos_cache: tuple[float, frozenset[str], tuple[str, ...]] | None = None


def is_connected() -> bool:
    return bool(get_settings().github_token.strip())


async def search_issues(query: str, *, limit: int = _SEARCH_LIMIT) -> str:
    items, _ = await _fetch(query, limit, restrict=True)
    if not items:
        return f"No issues or PRs in your repositories matched: {query}"
    listing = "\n".join(_format_item(it) for it in items)
    return f"{len(items)} result(s) in your repositories for `{query}`:\n{listing}"


async def my_work() -> str:
    sections: list[str] = []
    for label, query in _MY_WORK:
        items, total = await _fetch(query, 10, restrict=False)
        listing = "\n".join(_format_item(it) for it in items) if items else "(none)"
        sections.append(f"{label} ({total}):\n{listing}")
    return "\n\n".join(sections)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_settings().github_token.strip()}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
    }


def _raise_for_github(resp: httpx.Response) -> None:
    if resp.status_code == 401:
        raise RuntimeError("GitHub token is invalid or expired; re-issue the PAT.")
    if resp.status_code == 403:
        raise RuntimeError(
            "GitHub denied the request — the token lacks Issues / Pull requests / "
            "Metadata read access, or the rate limit was hit."
        )
    resp.raise_for_status()


async def _fetch(query: str, limit: int, *, restrict: bool) -> tuple[list[dict], int]:
    repos: frozenset[str] = frozenset()
    q = query
    per_page = limit
    if restrict:
        repos, owners = await _contributing_repos()
        # Narrow the search to the user's owners up front (cheap), unless the
        # query already carries its own repo/user/org scope.
        if owners and len(owners) <= _MAX_OWNER_QUALIFIERS and not _SCOPED_RE.search(query):
            q = f"{query} {' '.join(owners)}".strip()
        per_page = 100  # over-fetch, then post-filter to the exact repo set

    params = {"q": q, "per_page": per_page, "advanced_search": "true"}
    async with httpx.AsyncClient(timeout=_TIMEOUT, base_url=_API) as client:
        resp = await client.get("/search/issues", params=params, headers=_headers())
    _raise_for_github(resp)
    data = resp.json()
    items = list(data.get("items") or [])

    if restrict:
        items = [it for it in items if _repo_name(str(it.get("repository_url") or "")).lower() in repos]
        return items[:limit], len(items)
    return items[:limit], int(data.get("total_count") or 0)


async def _contributing_repos() -> tuple[frozenset[str], tuple[str, ...]]:
    """(repo full-names lowercased, owner search-qualifiers) for repos the user
    contributes to. Cached in-process for `_REPOS_TTL` seconds."""
    global _repos_cache
    now = time.monotonic()
    if _repos_cache is not None and now - _repos_cache[0] < _REPOS_TTL:
        return _repos_cache[1], _repos_cache[2]

    fullnames: set[str] = set()
    owners: dict[str, str] = {}  # login -> owner type ("User" | "Organization")
    async with httpx.AsyncClient(timeout=_TIMEOUT, base_url=_API) as client:
        for page in range(1, _REPO_PAGES + 1):
            resp = await client.get(
                "/user/repos",
                params={
                    "affiliation": "owner,collaborator,organization_member",
                    "per_page": 100,
                    "page": page,
                    "sort": "pushed",
                },
                headers=_headers(),
            )
            _raise_for_github(resp)
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                full = str(row.get("full_name") or "")
                if full:
                    fullnames.add(full.lower())
                owner = row.get("owner") or {}
                login = owner.get("login")
                if login and login not in owners:
                    owners[login] = str(owner.get("type") or "User")
            if len(rows) < 100:
                break

    quals = tuple(
        f"org:{login}" if otype == "Organization" else f"user:{login}"
        for login, otype in owners.items()
    )
    _repos_cache = (now, frozenset(fullnames), quals)
    return _repos_cache[1], _repos_cache[2]


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
