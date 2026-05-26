"""Gmail poll routine.

Refresh-on-demand, drain, persist next history_id. One account at a time
today — multi-account Gmail can be added by listing tokens (mirror Slack).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.auth import get_provider
from app.db.clients import oauth_tokens
from app.services.input.create import drain
from app.services.input.gmail.source import GmailSource

log = logging.getLogger(__name__)


def _empty(setup_error: str) -> dict:
    return {
        "fetched": 0,
        "agent_runs": 0,
        "tasks_created": 0,
        "skipped": 0,
        "errors": [{"setup": setup_error}],
    }


async def poll(session: Session, account_key: str | None) -> dict:
    token = oauth_tokens.get_decrypted(session, provider="google", account_key=account_key)
    if token is None:
        return _empty("no Google account connected")

    if token.is_expired:
        if not token.refresh_token:
            return _empty("Gmail access token expired; re-authorize")
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

    summary = await drain(gmail, session)

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
