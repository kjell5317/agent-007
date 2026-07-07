"""Mirror a kotx task's documents into the `documents` search cache.

Called from the kotx webhook and reconciliation poll. For each ingested task it
fetches TASK.md / REVIEW.md and the proposed PR title+body (whichever the task's
kind exposes), joins them into one document per kotx task (upsert key = the kotx
task id), and embeds it — refreshed on every transition, re-embedded only when
the combined text changed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import documents as documents_store
from app.services.input.embedding import embed
from app.services.input.kotx.normalize import INGESTED_KINDS
from app.services.kotx import client as kotx_client

log = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 8000


def _title(task: dict) -> str:
    repo = str(task.get("repo") or "").strip()
    number = task.get("subjectNumber")
    title = str(task.get("title") or "").strip()
    subject = f"{repo}#{number}".strip("#") if repo or number is not None else ""
    return f"{subject} {title}".strip() or (subject or title or f"kotx task {task.get('id')}")


def _content(task: dict, *, task_md: str | None, review_md: str | None, pr: dict | None) -> str:
    parts: list[str] = [f"# {_title(task)}"]
    if task_md:
        parts.append(task_md.strip())
    if review_md:
        parts.append(review_md.strip())
    if pr and (pr.get("title") or pr.get("body")):
        parts.append("## Proposed PR")
        if pr.get("title"):
            parts.append(str(pr["title"]).strip())
        if pr.get("body"):
            parts.append(str(pr["body"]).strip())
    return "\n\n".join(p for p in parts if p).strip()[:_MAX_CONTENT_CHARS]


def _metadata(task: dict) -> dict:
    meta = {
        "kotx_task_id": task.get("id"),
        "repo": task.get("repo"),
        "subject_number": task.get("subjectNumber"),
        "kind": task.get("kind"),
        "state": task.get("state"),
        "pr_number": task.get("prNumber") or task.get("trackedPrNumber"),
        "github_url": task.get("githubUrl"),
    }
    return {k: v for k, v in meta.items() if v is not None}


def _updated_at(task: dict) -> datetime:
    raw = task.get("updatedAt")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def cache_kotx_task(session: Session, task: dict) -> bool:
    """Fetch and cache a kotx task's documents. Returns True when a row was
    written. Flushes but does not commit — the caller owns the transaction."""
    if task.get("kind") not in INGESTED_KINDS:
        return False
    kotx_id = task.get("id")
    if kotx_id is None:
        return False

    kind = str(task.get("kind") or "")
    task_md = review_md = None
    pr: dict | None = None
    try:
        if kind in ("implement", "resolve_conflict"):
            task_md = await kotx_client.fetch_doc(int(kotx_id), "task")
        if kind == "review":
            review_md = await kotx_client.fetch_doc(int(kotx_id), "review")
        pr = await kotx_client.fetch_pr(int(kotx_id))
    except Exception:  # noqa: BLE001 — a transition still caches with whatever we got
        log.exception("kotx cache · doc fetch failed · task=%s", kotx_id)

    content = _content(task, task_md=task_md, review_md=review_md, pr=pr)
    if not content:
        return False

    external_id = str(kotx_id)
    existing = documents_store.get_by_external_id(
        session, provider="kotx", external_id=external_id
    )
    if existing is not None and existing.content == content and existing.embedding is not None:
        embedding = existing.embedding
    else:
        embedding = await embed(content)

    documents_store.upsert(
        session,
        provider="kotx",
        external_id=external_id,
        title=_title(task),
        snippet=_title(task),
        content=content,
        url=task.get("githubUrl"),
        metadata=_metadata(task),
        starts_at=None,
        ends_at=None,
        updated_at=_updated_at(task),
        embedding=embedding,
    )
    return True
