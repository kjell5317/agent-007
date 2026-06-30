from __future__ import annotations

from urllib.parse import quote, urlencode


def source_url_for_raw_input(raw) -> str | None:
    if raw is None:
        return None

    metadata = raw.source_metadata or {}
    if raw.source == "gmail":
        thread_id = metadata.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            return None

        thread = quote(thread_id.strip(), safe="")
        account = metadata.get("account")
        if isinstance(account, str) and account.strip():
            return (
                "https://mail.google.com/mail/?"
                f"{urlencode({'authuser': account.strip()})}#all/{thread}"
            )
        return f"https://mail.google.com/mail/u/0/#all/{thread}"

    if raw.source == "slack":
        channel_id = metadata.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return None

        message_ts: str | None = None
        if isinstance(raw.external_id, str) and ":" in raw.external_id:
            _, message_ts = raw.external_id.rsplit(":", 1)
        if not message_ts:
            thread_id = metadata.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                message_ts = thread_id.strip()

        params = {"channel": channel_id.strip()}
        if message_ts:
            params["message_ts"] = message_ts
        return f"https://slack.com/app_redirect?{urlencode(params)}"

    return None
