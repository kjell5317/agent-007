from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import get_settings

KOTX_ISSUE_REPOS: dict[str, str] = {
    "CSEE": "CSEE",
    "SocialAI": "Social Ai",
}
_TIMEOUT = 30.0


class KotxConfigError(RuntimeError):
    pass


class KotxRunError(RuntimeError):
    def __init__(self, detail: str, *, status_code: int = 502):
        super().__init__(detail)
        self.status_code = status_code


class KotxUnsupportedTaskError(ValueError):
    pass


@dataclass(frozen=True)
class KotxIssueRun:
    issue_url: str


def has_github_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.netloc.lower() in {"github.com", "www.github.com"}


def repo_for_label(label: str | None) -> str:
    if label not in KOTX_ISSUE_REPOS:
        raise KotxUnsupportedTaskError(
            "GitHub issue creation is only supported for CSEE and SocialAI tasks"
        )
    return KOTX_ISSUE_REPOS[label]


async def create_issue_run(task) -> KotxIssueRun:
    repo = repo_for_label(task.label)
    if has_github_url(task.link):
        raise KotxUnsupportedTaskError("Task already has a GitHub URL")

    settings = get_settings()
    base_url = settings.kotx_base_url.strip().rstrip("/")
    token = settings.kotx_api_token.strip()
    if not base_url or not token:
        raise KotxConfigError("kotx is not configured")

    payload = {
        "repo": repo,
        "title": task.title,
        "body": task.description or "",
    }
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
        try:
            response = await client.post(f"{base_url}/api/runs", json=payload)
        except httpx.HTTPError as exc:
            raise KotxRunError(f"kotx run creation failed: {exc}") from exc

    if response.status_code != 201:
        detail = _kotx_error_detail(response)
        raise KotxRunError(
            f"kotx run creation failed: {detail}",
            status_code=response.status_code,
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise KotxRunError("kotx run creation failed: response was not valid JSON") from exc

    issue_url = body.get("issueUrl") if isinstance(body, dict) else None
    if not isinstance(issue_url, str) or not issue_url:
        raise KotxRunError("kotx run creation failed: response did not include issueUrl")
    return KotxIssueRun(issue_url=issue_url)


def _kotx_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            message = payload.get(key)
            if isinstance(message, str) and message:
                return message
    return response.reason_phrase
