"""Gmail poll routine.

Refresh-on-demand, drain, persist next history_id. One account at a time
today — multi-account Gmail can be added by listing tokens (mirror Slack).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.auth.google_tokens import (
    GoogleReauthorizationRequired,
    GoogleTokenMissing,
    get_fresh_google_token,
)
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
    try:
        token = await get_fresh_google_token(session, account_key=account_key)
    except GoogleReauthorizationRequired:
        return _empty("Gmail access token expired; re-authorize")
    except GoogleTokenMissing:
        return _empty("no Google account connected")

    history_id = (token.extra or {}).get("history_id")
    log.debug(
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
