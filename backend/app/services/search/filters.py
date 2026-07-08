"""Pure query parsing for stage-1 suggest — no I/O, no DB.

`parse_query("rent source:gmail before:2026-06")` peels the `key:value` filter
tokens out and hands back the leftover free text plus a typed `Filters`. The
free text then becomes a prefix tsquery via `build_tsquery`. Keeping this pure
makes the whole matching contract testable from plain strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `key:value` where value has no whitespace. Case-insensitive keys. `source`
# is the single origin axis — it matches an input's source (gmail/slack/…) and
# a document's provider (calendar/notion/…) alike.
_FILTER_RE = re.compile(
    r"\b(source|label|is|before|after):(\S+)",
    re.IGNORECASE,
)

# `is:` synonyms → the raw_input status enum a task's latest anchor carries.
_STATUS_ALIASES = {
    "open": "open",
    "closed": "closed",
    "done": "closed",
    "complete": "closed",
    "completed": "closed",
    "not_task": "not_task",
    "nottask": "not_task",
    "dismissed": "not_task",
    "processing": "processing",
    "event": "event",
}

# Corpora each filter constrains to. A filter naming a corpus restricts the
# UNION to it; `before`/`after` are cross-corpus and don't restrict.
_TASK = "task"
_INPUT = "input"
_DOCUMENT = "document"
ALL_CORPORA = frozenset({_TASK, _INPUT, _DOCUMENT})


@dataclass(frozen=True)
class Filters:
    source: str | None = None  # input source OR document provider
    label: str | None = None
    status: str | None = None
    before: str | None = None  # ISO date boundary, exclusive upper bound
    after: str | None = None  # ISO date boundary, inclusive lower bound


def parse_query(query: str) -> tuple[str, Filters]:
    fields: dict[str, str | None] = {}

    def take(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        value = match.group(2)
        if key == "is":
            fields["status"] = _STATUS_ALIASES.get(value.lower())
        elif key in ("before", "after"):
            fields[key] = _date_boundary(value)
        else:
            fields[key] = value.lower() if key == "source" else value
        return " "

    text = _FILTER_RE.sub(take, query or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text, Filters(
        source=fields.get("source"),
        label=fields.get("label"),
        status=fields.get("status"),
        before=fields.get("before"),
        after=fields.get("after"),
    )


def _date_boundary(value: str) -> str | None:
    """Normalize `2026`, `2026-06`, `2026-06-07` to a `YYYY-MM-DD` boundary.

    Partial dates snap to the first day of their precision, so `before:2026-06`
    means "before June 1st". Junk values are dropped (return None) rather than
    breaking the query."""
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", value.strip())
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2) or 1)
    day = int(m.group(3) or 1)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def build_tsquery(text: str) -> str:
    """Free text → a `to_tsquery`-safe string, ANDing tokens with a `:*` prefix
    on the last so a half-typed final word still matches. Tokens are reduced to
    alphanumerics, sidestepping to_tsquery's operator syntax entirely."""
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    if not tokens:
        return ""
    tokens[-1] = tokens[-1] + ":*"
    return " & ".join(tokens)


def corpus_restriction(filters: Filters) -> frozenset[str] | None:
    """Which corpora a set of filters limits results to, or None for all.

    `source:` spans both origin-bearing corpora (inputs + documents); `is:` and
    `label:` are task concepts — naming one narrows the UNION to it."""
    restrict: set[str] = set()
    if filters.source:
        restrict.update((_INPUT, _DOCUMENT))
    if filters.status or filters.label:
        restrict.add(_TASK)
    return frozenset(restrict) or None
