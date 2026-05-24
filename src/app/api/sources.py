"""Source-driven ingestion endpoints.

For polling sources (Gmail today), the API exposes a trigger that fetches new
items, persists them, and runs the agent over each. Webhook sources should
register under `/inputs/{source}/webhook` instead.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.agent.runner import process_raw_input
from app.auth import get_provider
from app.db import get_session
from app.ingestion.gmail.source import GmailSource
from app.storage import oauth_tokens, raw_inputs

router = APIRouter(prefix="/sources", tags=["sources"])


@router.post("/gmail/poll")
async def gmail_poll(
    account_key: str | None = Query(None, description="Email of the connected Gmail account; defaults to most recent."),
    max_messages: int = Query(20, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    """Fetch new Gmail messages for the connected account and run the agent on each."""
    token = oauth_tokens.get_decrypted(session, provider="google", account_key=account_key)
    if token is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No Google account connected. Visit /oauth/google/authorize first.",
        )

    if token.is_expired:
        if not token.refresh_token:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Access token expired and no refresh_token on file — re-authorize.",
            )
        provider = get_provider("google")()
        bundle = await provider.refresh(token.refresh_token)
        oauth_tokens.upsert(
            session,
            provider="google",
            account_key=token.account_key,
            bundle=bundle,
            extra_merge=token.extra,
        )
        session.commit()
        token = oauth_tokens.get_decrypted(session, provider="google", account_key=token.account_key)
        assert token is not None

    history_id = (token.extra or {}).get("history_id")
    source = GmailSource(
        account_key=token.account_key,
        access_token=token.access_token,
        history_id=history_id,
    )

    summary = {"fetched": 0, "agent_runs": 0, "tasks_created": 0, "skipped": 0, "errors": []}

    async for envelope in source.fetch():
        summary["fetched"] += 1

        raw = raw_inputs.create(session, envelope)
        session.commit()

        if raw.processed_at is not None:
            # Already seen + processed in a prior run.
            continue

        try:
            trace = await process_raw_input(session, raw.id)
            summary["agent_runs"] += 1
            outcome = trace.get("outcome")
            if outcome == "task_created":
                summary["tasks_created"] += 1
            elif outcome in {"duplicate", "not_task", "closed", "no_change", "updated"}:
                summary["skipped"] += 1
        except Exception as exc:  # noqa: BLE001 — best-effort batch processing
            summary["errors"].append({"external_id": envelope.external_id, "error": str(exc)})
            session.rollback()

        if summary["agent_runs"] + len(summary["errors"]) >= max_messages:
            break

    if source.next_history_id:
        oauth_tokens.set_extra(
            session,
            provider="google",
            account_key=token.account_key,
            patch={"history_id": source.next_history_id, "last_polled_at": datetime.now(timezone.utc).isoformat()},
        )
        session.commit()

    return summary
