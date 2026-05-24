"""Source-driven ingestion endpoint.

One trigger for all polling sources: `POST /sources/poll`. Omit `?source=` to
fan out across every connected source; pass it to narrow to one. Pass
`?account_key=` (requires `?source=`) to narrow further to a single account.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.agent.runner import process_raw_input
from app.auth import get_provider
from app.db import get_session
from app.ingestion.gmail.source import GmailSource
from app.ingestion.slack.source import SlackSource
from app.notifications import notify_error
from app.storage import oauth_tokens, raw_inputs

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sources", tags=["sources"])

_SUPPORTED_SOURCES = ("gmail", "slack")


@router.post("/poll")
async def poll(
    source: str | None = Query(
        None, description=f"One of {_SUPPORTED_SOURCES}. Omit to poll all connected sources."
    ),
    account_key: str | None = Query(
        None, description="Narrow to a single account. Requires `source`."
    ),
    session: Session = Depends(get_session),
) -> dict:
    if account_key and not source:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "`account_key` requires `source` — pick one source to filter within.",
        )
    if source and source not in _SUPPORTED_SOURCES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown source {source!r}. Supported: {_SUPPORTED_SOURCES}.",
        )

    targets = [source] if source else list(_SUPPORTED_SOURCES)

    aggregate: dict = {
        "fetched": 0,
        "agent_runs": 0,
        "tasks_created": 0,
        "skipped": 0,
        "errors": [],
        "per_source": {},
    }

    for name in targets:
        log.info("poll dispatch · source=%s account=%s", name, account_key or "(all)")
        if name == "gmail":
            result = await _poll_gmail(session, account_key)
        elif name == "slack":
            result = await _poll_slack(session, account_key)
        else:  # unreachable thanks to validation above
            continue

        aggregate["per_source"][name] = result
        for k in ("fetched", "agent_runs", "tasks_created", "skipped"):
            aggregate[k] += result.get(k, 0)
        aggregate["errors"].extend(result.get("errors", []))

    return aggregate


# --- per-source helpers -------------------------------------------------------


async def _poll_gmail(session: Session, account_key: str | None) -> dict:
    """Refresh-on-demand, drain, persist next history_id. One account at a time
    today — multi-account Gmail can be added by listing tokens (mirror Slack)."""
    token = oauth_tokens.get_decrypted(session, provider="google", account_key=account_key)
    if token is None:
        return {"fetched": 0, "agent_runs": 0, "tasks_created": 0, "skipped": 0,
                "errors": [{"setup": "no Google account connected"}]}

    if token.is_expired:
        if not token.refresh_token:
            return {"fetched": 0, "agent_runs": 0, "tasks_created": 0, "skipped": 0,
                    "errors": [{"setup": "Gmail access token expired; re-authorize"}]}
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
        token = oauth_tokens.get_decrypted(
            session, provider="google", account_key=token.account_key
        )
        assert token is not None

    history_id = (token.extra or {}).get("history_id")
    log.info(
        "gmail poll start · account=%s history_id=%s",
        token.account_key, history_id or "(bootstrap)",
    )
    gmail = GmailSource(
        account_key=token.account_key,
        access_token=token.access_token,
        history_id=history_id,
    )

    summary = await _drain(gmail, session)

    log.info(
        "gmail poll done · account=%s fetched=%d created=%d skipped=%d errors=%d",
        token.account_key,
        summary["fetched"], summary["tasks_created"], summary["skipped"],
        len(summary["errors"]),
    )

    if gmail.next_history_id:
        oauth_tokens.set_extra(
            session,
            provider="google",
            account_key=token.account_key,
            patch={
                "history_id": gmail.next_history_id,
                "last_polled_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        session.commit()

    return summary


async def _poll_slack(session: Session, account_key: str | None) -> dict:
    """Loop over Slack workspaces; aggregate per-account summaries."""
    if account_key is not None:
        account_keys = [account_key]
    else:
        account_keys = oauth_tokens.list_account_keys(session, provider="slack")

    if not account_keys:
        return {"fetched": 0, "agent_runs": 0, "tasks_created": 0, "skipped": 0,
                "errors": [{"setup": "no Slack workspace connected"}]}

    log.info("slack poll start · %d workspace(s): %s", len(account_keys), account_keys)
    aggregate: dict = {
        "fetched": 0, "agent_runs": 0, "tasks_created": 0, "skipped": 0,
        "errors": [], "per_account": {},
    }

    for ak in account_keys:
        token = oauth_tokens.get_decrypted(session, provider="slack", account_key=ak)
        if token is None:
            continue

        extra = token.extra or {}
        log.info(
            "slack poll · account=%s known_watermarks=%d",
            ak, len(extra.get("channels") or {}),
        )
        source = SlackSource(
            account_key=token.account_key,
            access_token=token.access_token,
            authed_user_id=extra.get("authed_user_id"),
            watermarks=extra.get("channels") or {},
        )

        summary = await _drain(source, session)
        log.info(
            "slack poll workspace done · account=%s fetched=%d created=%d skipped=%d errors=%d",
            ak,
            summary["fetched"], summary["tasks_created"], summary["skipped"],
            len(summary["errors"]),
        )
        aggregate["per_account"][ak] = summary
        for k in ("fetched", "agent_runs", "tasks_created", "skipped"):
            aggregate[k] += summary[k]
        aggregate["errors"].extend(summary["errors"])

        if source.next_watermarks:
            oauth_tokens.set_extra(
                session,
                provider="slack",
                account_key=token.account_key,
                patch={
                    "channels": source.next_watermarks,
                    "last_polled_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            session.commit()

    return aggregate


async def _drain(source, session: Session) -> dict:
    """Common poll loop: drain the source, persist envelopes, run the agent.

    Two error surfaces, both caught + notified + recorded in `summary` rather
    than 500'd:
      - the source iterator itself (auth/scope/API errors raised by fetch())
      - per-message agent errors
    """
    summary: dict = {"fetched": 0, "agent_runs": 0, "tasks_created": 0, "skipped": 0, "errors": []}
    source_name = getattr(source, "name", type(source).__name__)

    try:
        async for envelope in source.fetch():
            summary["fetched"] += 1
            subject = (envelope.source_metadata or {}).get("subject")
            log.info(
                "envelope · %s external_id=%s%s",
                envelope.source,
                envelope.external_id,
                f" subject={subject!r}" if subject else "",
            )

            raw = raw_inputs.create(session, envelope)
            session.commit()

            if raw.processed_at is not None:
                log.debug("skip · already-processed raw=%s", raw.id)
                continue

            try:
                trace = await process_raw_input(session, raw.id)
                summary["agent_runs"] += 1
                outcome = trace.get("outcome")
                if outcome == "task_created":
                    summary["tasks_created"] += 1
                elif outcome in {"duplicate", "not_task", "closed", "no_change", "updated"}:
                    summary["skipped"] += 1
                log.info(
                    "agent · raw=%s outcome=%s task_id=%s",
                    raw.id,
                    outcome,
                    trace.get("task_id") or trace.get("existing_task_id") or "—",
                )
            except Exception as exc:  # noqa: BLE001 — best-effort batch processing
                summary["errors"].append({"external_id": envelope.external_id, "error": str(exc)})
                session.rollback()
                log.exception("agent error · raw=%s external_id=%s", raw.id, envelope.external_id)
                await notify_error(
                    f"Agent error ({envelope.source})",
                    exc,
                    context=f"external_id={envelope.external_id}",
                )
    except Exception as exc:  # noqa: BLE001 — source fetch failed (auth, scope, network, ...)
        session.rollback()
        summary["errors"].append({"source_fetch": str(exc)})
        log.exception("source fetch failed · source=%s fetched=%d", source_name, summary["fetched"])
        await notify_error(
            f"Source fetch error ({source_name})",
            exc,
            context=f"fetched={summary['fetched']} before failure",
        )

    return summary
