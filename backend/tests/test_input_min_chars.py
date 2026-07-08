"""The ingestion character floor: `drain` drops envelopes whose cleaned content
is below `min_input_chars` before they're persisted — except kotx, which is
short + structured and handled deterministically."""

from __future__ import annotations

from app.config import get_settings
from app.db.schemas.raw_input import RawInputCreate
from app.services.input.create import _below_min_chars


def _env(source: str, content: str) -> RawInputCreate:
    return RawInputCreate(source=source, external_id="x", content=content, source_metadata={})


def test_short_conversational_input_is_below_floor():
    assert _below_min_chars(_env("gmail", "ok")) is True
    assert _below_min_chars(_env("slack", "thanks!")) is True
    # Whitespace-only content counts as empty.
    assert _below_min_chars(_env("gmail", "        ")) is True


def test_content_at_or_above_floor_is_kept():
    body = "x" * get_settings().min_input_chars
    assert _below_min_chars(_env("gmail", body)) is False
    assert _below_min_chars(_env("slack", "Please review the Q3 budget doc")) is False


def test_kotx_is_never_dropped():
    assert _below_min_chars(_env("kotx", "ok")) is False
    assert _below_min_chars(_env("kotx", "")) is False
