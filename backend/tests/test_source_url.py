from types import SimpleNamespace

from app.services.source_url import source_url_for_raw_input


def test_gmail_source_url_uses_account_and_thread_id():
    raw = SimpleNamespace(
        source="gmail",
        external_id="msg-1",
        source_metadata={
            "account": "me@example.com",
            "thread_id": "thread-123",
        },
    )

    assert (
        source_url_for_raw_input(raw)
        == "https://mail.google.com/mail/?authuser=me%40example.com#all/thread-123"
    )


def test_slack_source_url_uses_channel_and_external_message_ts():
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
