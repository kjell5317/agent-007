from __future__ import annotations

from urllib.parse import quote, urlencode


def source_url_for_raw_input(raw) -> str | None:
    if raw is None:
        return None

    metadata = raw.source_metadata or {}

    # A permalink captured at ingestion (Slack's chat.getPermalink today) is the
    # source's own canonical URL — always prefer it over anything we rebuild.
    permalink = metadata.get("permalink")
    if isinstance(permalink, str) and permalink.strip():
        return permalink.strip()

    if raw.source == "gmail":
        account = metadata.get("account")
        authuser = account.strip() if isinstance(account, str) and account.strip() else None

        # The web UI's thread slug (e.g. FMfcg…) can't be derived from the API's
        # threadId, and a bare threadId in #all/ no longer reliably resolves. An
        # rfc822msgid: search jumps straight to the message and is stable across
        # Gmail's id changes, so prefer it; fall back to the thread id.
        message_id = metadata.get("message_id_header")
        if isinstance(message_id, str) and message_id.strip():
            query = "rfc822msgid:" + message_id.strip().strip("<>")
            fragment = f"search/{quote(query, safe=':')}"
        else:
            thread_id = metadata.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id.strip():
                return None
            fragment = f"all/{quote(thread_id.strip(), safe='')}"

        if authuser:
            return f"https://mail.google.com/mail/?{urlencode({'authuser': authuser})}#{fragment}"
        return f"https://mail.google.com/mail/u/0/#{fragment}"

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
