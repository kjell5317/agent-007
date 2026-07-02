from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import get_settings

GITHUB_LABEL_REPOS: dict[str, str] = {
    "CSEE": "askLio/CSEE-strategic-negotiation-agent",
    "Social AI": "TUM-Social-AI/AflaConnect",
}
GITHUB_ASSIGNEE = "kjell5317"
_BASE_URL = "https://api.github.com"
_TIMEOUT = 20.0


class GitHubConfigError(RuntimeError):
    pass


class GitHubIssueError(RuntimeError):
    pass


class GitHubUnsupportedTaskError(ValueError):
    pass


@dataclass(frozen=True)
class GitHubIssue:
    html_url: str


def has_github_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.netloc.lower() in {"github.com", "www.github.com"}


def repo_for_label(label: str | None) -> str:
    if label not in GITHUB_LABEL_REPOS:
        raise GitHubUnsupportedTaskError("GitHub issue creation is only supported for CSEE and Social AI tasks")
    return GITHUB_LABEL_REPOS[label]


async def create_task_issue(task) -> GitHubIssue:
    repo = repo_for_label(task.label)
    token = get_settings().github_token.strip()
    if not token:
        raise GitHubConfigError("GitHub token is not configured")
    if has_github_url(task.link):
        raise GitHubUnsupportedTaskError("Task already has a GitHub URL")

    payload = {
        "title": task.title,
        "body": task.description or "",
        "assignees": [GITHUB_ASSIGNEE],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
        try:
            response = await client.post(f"{_BASE_URL}/repos/{repo}/issues", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _github_error_detail(exc.response)
            raise GitHubIssueError(f"GitHub issue creation failed: {detail}") from exc
        except httpx.HTTPError as exc:
            raise GitHubIssueError(f"GitHub issue creation failed: {exc}") from exc

    html_url = response.json().get("html_url")
    if not isinstance(html_url, str) or not html_url:
        raise GitHubIssueError("GitHub issue creation failed: response did not include html_url")
    return GitHubIssue(html_url=html_url)


def _github_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    message = payload.get("message") if isinstance(payload, dict) else None
    return message if isinstance(message, str) and message else response.reason_phrase
