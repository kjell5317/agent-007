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
        # Deep-link the thread in the Gmail web UI. For GitHub-relabelled
        # notifications the real Gmail thread id lives under `gmail_thread_id`
        # (`thread_id` carries the canonical `github:…` key instead).
        thread_id = metadata.get("gmail_thread_id") or metadata.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            return None
        return f"https://mail.google.com/mail/u/0/#all/{quote(thread_id.strip(), safe='')}"

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
