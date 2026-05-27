from app.services.input.slack.preprocess import preprocess_message


def test_slack_preprocess_strips_markdown_and_prefers_hrefs() -> None:
    result = preprocess_message(
        {
            "text": (
                "<!channel> *please* review [the plan](https://example.com/plan) "
                "and <https://slack.com/docs|Slack docs> with "
                "<mailto:boss@example.com|Boss> :wave:"
            ),
            "user": "U1",
            "ts": "1.0",
        },
        channel_id="C1",
        workspace_name="Acme",
        user_names={"U1": "alice"},
    )

    assert result.body == (
        "@channel please review https://example.com/plan and "
        "https://slack.com/docs with boss@example.com"
    )
    assert result.metadata["urls"] == ["https://slack.com/docs", "https://example.com/plan"]
    assert result.metadata["from"] == "alice (Acme)"
    assert result.metadata["channel_id"] == "C1"


def test_slack_preprocess_drops_emoji_only_content() -> None:
    result = preprocess_message(
        {"text": ":white_check_mark: :party-parrot:", "user": "U1", "ts": "1.0"},
        channel_id="C1",
        user_names={"U1": "alice"},
    )

    assert result.body == ""


def test_slack_preprocess_keeps_underscores_inside_words_and_urls() -> None:
    result = preprocess_message(
        {
            "text": (
                "_Important_: use snake_case and "
                "[query](https://example.com/a_path?field=snake_case)"
            ),
            "user": "U1",
            "ts": "1.0",
        },
        channel_id="C1",
        user_names={"U1": "alice"},
    )

    assert result.body == (
        "Important: use snake_case and https://example.com/a_path?field=snake_case"
    )
