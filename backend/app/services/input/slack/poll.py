"""Slack poll routine.

Loop over Slack workspaces; aggregate per-account summaries. Pass an
explicit `account_key` to narrow to a single workspace.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import oauth_tokens
from app.services.input.create import drain
from app.services.input.slack.source import SlackSource

log = logging.getLogger(__name__)


def _empty_setup(error: str) -> dict:
    return {
        "fetched": 0,
        "agent_runs": 0,
        "tasks_created": 0,
        "skipped": 0,
        "errors": [{"setup": error}],
    }


async def poll(session: Session, account_key: str | None) -> dict:
    if account_key is not None:
        account_keys = [account_key]
    else:
        account_keys = oauth_tokens.list_account_keys(session, provider="slack")

    if not account_keys:
        return _empty_setup("no Slack workspace connected")

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

        summary = await drain(source, session)
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
