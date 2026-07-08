"""GitHub search service, network-free: swap httpx.AsyncClient for a fake that
returns canned REST responses, so we exercise query shaping, formatting, and
error mapping against real httpx.Response objects."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.services import github


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://api.github.com/search/issues"),
    )


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._response


def _install(monkeypatch, response: httpx.Response, *, token: str = "ghp_x") -> _FakeClient:
    fake = _FakeClient(response)
    monkeypatch.setattr(github, "get_settings", lambda: SimpleNamespace(github_token=token))
    monkeypatch.setattr(github.httpx, "AsyncClient", lambda *a, **kw: fake)
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
async def test_search_shapes_query_and_formats_issue(monkeypatch):
    fake = _install(monkeypatch, _response(200, {"total_count": 1, "items": [_issue()]}))
    out = await github.search_issues("is:open assignee:@me")

    assert fake.calls[0]["params"]["q"] == "is:open assignee:@me"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer ghp_x"
    assert "[acme/widgets#7]" in out
    assert "issue, open, by octocat" in out
    assert "https://github.com/acme/widgets/issues/7" in out
    assert "line one line two" in out  # body newlines collapsed


@pytest.mark.asyncio
async def test_pull_request_state_merged(monkeypatch):
    item = _issue(
        number=9,
        pull_request={"merged_at": "2026-07-02T00:00:00Z"},
        html_url="https://github.com/acme/widgets/pull/9",
    )
    _install(monkeypatch, _response(200, {"total_count": 1, "items": [item]}))
    out = await github.search_issues("is:pr author:@me")
    assert "#9]" in out
    assert "PR, merged, by octocat" in out


@pytest.mark.asyncio
async def test_no_matches(monkeypatch):
    _install(monkeypatch, _response(200, {"total_count": 0, "items": []}))
    out = await github.search_issues("nonsense")
    assert out == "No GitHub issues or PRs matched: nonsense"


@pytest.mark.asyncio
async def test_my_work_runs_assigned_and_review_queries(monkeypatch):
    fake = _install(monkeypatch, _response(200, {"total_count": 0, "items": []}))
    out = await github.my_work()
    queries = [c["params"]["q"] for c in fake.calls]
    assert queries == ["is:open assignee:@me", "is:open review-requested:@me"]
    assert "Assigned to you (0):" in out
    assert "Review requested from you (0):" in out
    assert "(none)" in out


@pytest.mark.asyncio
async def test_auth_error_maps_to_clear_message(monkeypatch):
    _install(monkeypatch, _response(401, {"message": "Bad credentials"}))
    with pytest.raises(RuntimeError, match="invalid or expired"):
        await github.search_issues("x")


@pytest.mark.asyncio
async def test_forbidden_maps_to_scope_hint(monkeypatch):
    _install(monkeypatch, _response(403, {"message": "Forbidden"}))
    with pytest.raises(RuntimeError, match="Issues / Pull requests"):
        await github.search_issues("x")
