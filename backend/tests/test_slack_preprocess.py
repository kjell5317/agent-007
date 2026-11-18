from __future__ import annotations

import pytest

from app.services.input.slack import source as slack_source
from app.services.input.slack.preprocess import preprocess_message, referenced_user_ids
from app.services.input.slack.source import SlackSource


def _body(text: str, user_names: dict[str, str] | None = None) -> str:
    return preprocess_message(
        {"text": text, "user": "U_SENDER"}, channel_id="C1", user_names=user_names or {},
    ).body


def test_mentions_render_display_names_from_the_user_map():
    assert _body("ping <@U012AB> and <@W99X>", {"U012AB": "alice", "W99X": "bob"}) == "ping @alice and @bob"


def test_inline_mention_label_wins_and_unknown_ids_stay_visible():
    assert _body("<@U1|carol> <@U2>", {"U1": "ignored"}) == "@carol @U2"


def test_sender_is_the_display_name_with_workspace():
    result = preprocess_message(
        {"text": "hi", "user": "U1"}, channel_id="C1", workspace_name="Acme",
        user_names={"U1": "alice"},
    )
    assert result.metadata["from"] == "alice (Acme)"


def test_referenced_user_ids_lists_sender_and_unlabelled_mentions():
    message = {"text": "<@U1> <@U2|bob> <@W3>", "user": "U4"}
    assert referenced_user_ids(message) == {"U1", "W3", "U4"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("thanks :+1::skin-tone-2: see you", "thanks see you"),
        (":tada::tada: shipped", "shipped"),
        (":white_check_mark: done", "done"),
        ("great :heart_eyes:!", "great !"),
        ("ok :-1:", "ok"),
    ],
)
def test_emoji_shortcodes_are_stripped(text, expected):
    assert _body(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "meet at 10:30:00 sharp",
        "std::vector::push_back",
        "ratio key:value:other",
        "keep :Capital: words",
    ],
)
def test_text_that_only_looks_like_emoji_survives(text):
    assert _body(text) == text


def test_urls_keep_their_colons():
    body = _body("see <https://example.com/a:b:c/:id:/x|docs> and https://x.io/:tada:/y :tada:")
    assert body == "see https://example.com/a:b:c/:id:/x and https://x.io/:tada:/y"


class _NamesClient:
    def __init__(self):
        self.lookups: list[str] = []

    async def users_conversations(self, **kwargs):
        yield {"id": "D1", "is_im": True, "user": "U0PEER"}

    async def conversations_history(self, channel, *, oldest=None):
        yield {"ts": "1893456000.000100", "user": "U0PEER", "text": "ask <@U0MATE> :eyes:"}

    async def users_info(self, user_id):
        self.lookups.append(user_id)
        names = {"U0PEER": "Peer", "U0MATE": "Mate"}
        return {"profile": {"display_name": names[user_id]}} if user_id in names else None

    async def get_permalink(self, channel, message_ts):
        return None


@pytest.mark.asyncio
async def test_fetch_resolves_sender_mentions_and_dm_label_once_across_polls(monkeypatch):
    monkeypatch.setattr(slack_source, "_user_names", {})
    client = _NamesClient()

    async def poll() -> list:
        source = SlackSource(account_key="T1", access_token="xoxp-test", authed_user_id="U_ME")
        source.client = client
        return [e async for e in source.fetch()]

    first = await poll()
    second = await poll()

    envelope = first[0]
    assert envelope.content == "ask @Mate"
    assert envelope.source_metadata["from"] == "Peer"
    assert envelope.source_metadata["channel_name"] == "DM:Peer"
    assert second[0].content == "ask @Mate"
    assert sorted(client.lookups) == ["U0MATE", "U0PEER"]
