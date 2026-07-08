"""GitHub search service, network-free: route a fake httpx client by URL so we
exercise contributing-repo scoping, query shaping, formatting, and error mapping
against real httpx.Response objects."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.services import github


def _response(status: int, payload) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://api.github.com/"),
    )


class _FakeClient:
    def __init__(self, *, search: httpx.Response, repos: httpx.Response):
        self._search = search
        self._repos = repos
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._repos if url == "/user/repos" else self._search


def _repo(full_name: str, owner_type: str = "User") -> dict:
    return {"full_name": full_name, "owner": {"login": full_name.split("/")[0], "type": owner_type}}


def _install(
    monkeypatch,
    *,
    search: httpx.Response,
    repos: httpx.Response | None = None,
    token: str = "ghp_x",
) -> _FakeClient:
    fake = _FakeClient(search=search, repos=repos if repos is not None else _response(200, []))
    monkeypatch.setattr(github, "get_settings", lambda: SimpleNamespace(github_token=token))
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda *a, **kw: fake)
    monkeypatch.setattr(github, "_repos_cache", None)  # avoid cross-test leakage
    return fake


def _issue(**kw) -> dict:
    base = {
        "number": 7,
        "title": "Fix the thing",
        "state": "open",
        "user": {"login": "octocat"},
        "html_url": "https://github.com/acme/widgets/issues/7",
        "repository_url": "https://api.github.com/repos/acme/widgets",
        "updated_at": "2026-07-01T10:00:00Z",
        "body": "line one\nline two",
    }
    base.update(kw)
    return base


def test_is_connected_reflects_token(monkeypatch):
    monkeypatch.setattr(github, "get_settings", lambda: SimpleNamespace(github_token="  "))
    assert github.is_connected() is False
    monkeypatch.setattr(github, "get_settings", lambda: SimpleNamespace(github_token="ghp_x"))
    assert github.is_connected() is True


@pytest.mark.asyncio
async def test_search_scopes_to_owner_and_formats(monkeypatch):
    fake = _install(
        monkeypatch,
        search=_response(200, {"total_count": 1, "items": [_issue()]}),
        repos=_response(200, [_repo("acme/widgets", "Organization")]),
    )
    out = await github.search_issues("is:open assignee:@me")

    # Contributing repos fetched first, then the (owner-scoped) search.
    assert fake.calls[0]["url"] == "/user/repos"
    search_call = next(c for c in fake.calls if c["url"] == "/search/issues")
    assert search_call["params"]["q"] == "is:open assignee:@me org:acme"
    assert search_call["headers"]["Authorization"] == "Bearer ghp_x"
    assert "[acme/widgets#7]" in out
    assert "issue, open, by octocat" in out
    assert "line one line two" in out  # body newlines collapsed


@pytest.mark.asyncio
async def test_search_filters_out_non_contributing_repos(monkeypatch):
    mine = _issue(number=1, repository_url="https://api.github.com/repos/acme/widgets")
    theirs = _issue(number=2, repository_url="https://api.github.com/repos/stranger/other")
    _install(
        monkeypatch,
        search=_response(200, {"total_count": 2, "items": [theirs, mine]}),
        repos=_response(200, [_repo("acme/widgets")]),
    )
    out = await github.search_issues("bug")
    assert "acme/widgets#1" in out
    assert "stranger/other" not in out


@pytest.mark.asyncio
async def test_search_leaves_explicit_scope_untouched(monkeypatch):
    fake = _install(
        monkeypatch,
        search=_response(200, {"total_count": 1, "items": [_issue()]}),
        repos=_response(200, [_repo("acme/widgets")]),
    )
    await github.search_issues("repo:acme/widgets is:pr")
    search_call = next(c for c in fake.calls if c["url"] == "/search/issues")
    # Query already scoped → no owner qualifier appended.
    assert search_call["params"]["q"] == "repo:acme/widgets is:pr"


@pytest.mark.asyncio
async def test_pull_request_state_merged(monkeypatch):
    item = _issue(
        number=9,
        pull_request={"merged_at": "2026-07-02T00:00:00Z"},
        html_url="https://github.com/acme/widgets/pull/9",
    )
    _install(
        monkeypatch,
        search=_response(200, {"total_count": 1, "items": [item]}),
        repos=_response(200, [_repo("acme/widgets")]),
    )
    out = await github.search_issues("is:pr author:@me")
    assert "#9]" in out
    assert "PR, merged, by octocat" in out


@pytest.mark.asyncio
async def test_no_matches(monkeypatch):
    _install(
        monkeypatch,
        search=_response(200, {"total_count": 0, "items": []}),
        repos=_response(200, [_repo("acme/widgets")]),
    )
    out = await github.search_issues("nonsense")
    assert out == "No issues or PRs in your repositories matched: nonsense"


@pytest.mark.asyncio
async def test_my_work_is_not_repo_scoped(monkeypatch):
    fake = _install(monkeypatch, search=_response(200, {"total_count": 0, "items": []}))
    out = await github.my_work()
    # my_work is self-scoped (assignee/review) — it never fetches /user/repos.
    assert all(c["url"] == "/search/issues" for c in fake.calls)
    assert [c["params"]["q"] for c in fake.calls] == [
        "is:open assignee:@me",
        "is:open review-requested:@me",
    ]
    assert "Assigned to you (0):" in out
    assert "Review requested from you (0):" in out


@pytest.mark.asyncio
async def test_auth_error_maps_to_clear_message(monkeypatch):
    _install(monkeypatch, search=_response(401, {}), repos=_response(401, {}))
    with pytest.raises(RuntimeError, match="invalid or expired"):
        await github.search_issues("x")


@pytest.mark.asyncio
async def test_forbidden_maps_to_scope_hint(monkeypatch):
    _install(monkeypatch, search=_response(403, {}), repos=_response(403, {}))
    with pytest.raises(RuntimeError, match="Issues / Pull requests"):
        await github.search_issues("x")
