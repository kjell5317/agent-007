from types import SimpleNamespace

from app.services.source_url import source_url_for_raw_input


def test_gmail_source_url_links_thread_in_web_ui():
    raw = SimpleNamespace(
        source="gmail",
        external_id="msg-1",
        source_metadata={
            "account": "me@example.com",
            "thread_id": "thread-123",
            "message_id_header": "<CAF+abc@mail.gmail.com>",
        },
    )

    assert (
        source_url_for_raw_input(raw)
        == "https://mail.google.com/mail/u/0/#all/thread-123"
    )


def test_gmail_source_url_prefers_gmail_thread_id_for_github_relabelled():
    # GitHub notifications get `thread_id` rewritten to the canonical github key;
    # the real Gmail thread stays under `gmail_thread_id`.
    raw = SimpleNamespace(
        source="gmail",
        external_id="msg-1",
        source_metadata={
            "thread_id": "github:owner/repo#42",
            "gmail_thread_id": "thread-123",
        },
    )

    assert (
        source_url_for_raw_input(raw)
        == "https://mail.google.com/mail/u/0/#all/thread-123"
    )


def test_gmail_source_url_none_without_thread():
    raw = SimpleNamespace(source="gmail", external_id="msg-1", source_metadata={})

    assert source_url_for_raw_input(raw) is None


def test_slack_source_url_prefers_stored_permalink():
    raw = SimpleNamespace(
        source="slack",
        external_id="C123:1710000000.000100",
        source_metadata={
            "channel_id": "C123",
            "permalink": "https://acme.slack.com/archives/C123/p1710000000000100",
        },
    )

    assert (
        source_url_for_raw_input(raw)
        == "https://acme.slack.com/archives/C123/p1710000000000100"
    )


def test_slack_source_url_falls_back_to_app_redirect_without_permalink():
    raw = SimpleNamespace(
        source="slack",
        external_id="C123:1710000000.000100",
        source_metadata={
            "channel_id": "C123",
            "thread_id": "1710000000.000000",
        },
    )

    assert (
        source_url_for_raw_input(raw)
        == "https://slack.com/app_redirect?channel=C123&message_ts=1710000000.000100"
    )


def test_source_url_is_none_when_provider_metadata_is_missing():
    raw = SimpleNamespace(source="gmail", external_id="msg-1", source_metadata={})

    assert source_url_for_raw_input(raw) is None
