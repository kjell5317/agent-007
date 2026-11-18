"""Relevance across sources (pure).

Results from different places carry scores that can't be compared — an RRF
fusion from Postgres, a Drive listing, a GitHub search. So the Enter search
re-scores every candidate the same way and fuses three rankings by rank
(Reciprocal Rank Fusion, like the local hybrid search):

  * semantic — cosine similarity between the query embedding and the
    candidate's (stored, or embedded from title + snippet);
  * lexical — how well each query word matches a word in the candidate's
    title and snippet, typo-tolerant via trigram overlap;
  * time — how far the candidate's date lies from now, in either direction
    (see `time_distance`).

Ties keep the candidates' incoming order.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

RRF_K = 60
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


_DAY = 86_400.0


@dataclass
class Candidate:
    hit: Any
    text: str
    semantic: float | None = None
    # Seconds between the hit's date and now (see `time_distance`).
    distance: float | None = None


def words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _trigrams(word: str) -> set[str]:
    padded = f"  {word} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def word_match(query_word: str, word: str) -> float:
    # A query word matching the start of a word counts fully: "presentat"
    # finds "presentation".
    if word == query_word or word.startswith(query_word):
        return 1.0
    a, b = _trigrams(query_word), _trigrams(word)
    return len(a & b) / len(a | b)


def lexical_score(query: str, text: str) -> float:
    """Mean over query words of their best match in `text` (0–1)."""
    q_words = words(query)
    t_words = set(words(text))
    if not q_words or not t_words:
        return 0.0
    return sum(max(word_match(q, t) for t in t_words) for q in q_words) / len(q_words)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def time_distance(hit: Any, now: datetime, query: str) -> float | None:
    """How far a hit's date lies from `now`, in seconds — far ahead counts
    exactly like far behind. Tasks are dated by their due date; events by their
    start, messages by arrival, notes, files and GitHub items by their last
    change (each source's `ts`). A contact has no date, so its match quality
    stands in, in days, mirroring the frontend's `searchDistance`."""
    if hit.type == "contact":
        return _contact_distance(hit, query)
    due = (hit.meta or {}).get("due_date") if hit.type == "task" else None
    when = datetime.fromisoformat(due) if due else hit.ts
    return abs((now - when).total_seconds()) if when is not None else None


def _contact_distance(hit: Any, query: str) -> float:
    q = query.strip().lower()
    meta = hit.meta or {}
    name = hit.title.lower()
    emails = [email.lower() for email in meta.get("emails") or []]
    phones = [re.sub(r"\D", "", phone) for phone in meta.get("phones") or []]
    phone_query = re.sub(r"\D", "", q)
    name_words = name.split()
    if name == q or q in emails:
        return 0.0
    if name.startswith(q) or any(email.startswith(q) for email in emails):
        return _DAY
    if all(any(part.startswith(word) for part in name_words) for word in q.split()):
        return _DAY * 2
    if q in name or (len(phone_query) >= 3 and any(phone_query in phone for phone in phones)):
        return _DAY * 3
    if any(q in email for email in emails) or q in (meta.get("org") or "").lower():
        return _DAY * 5
    return _DAY * 14


def rank(query: str, candidates: list[Candidate], *, min_lexical: float = 0.3) -> list[Candidate]:
    """Candidates by fused relevance, best first. Lexical matches below
    `min_lexical` don't count as a match at all; undated candidates sit out
    the time ranking."""
    lexical = [lexical_score(query, c.text) for c in candidates]
    fused = [0.0] * len(candidates)
    rankings = (
        {i: -c.semantic for i, c in enumerate(candidates) if c.semantic is not None},
        {i: -lexical[i] for i in range(len(candidates)) if lexical[i] >= min_lexical},
        {i: c.distance for i, c in enumerate(candidates) if c.distance is not None},
    )
    for keys in rankings:
        for i, position in _positions(keys).items():
            fused[i] += 1.0 / (RRF_K + position)
    order = sorted(range(len(candidates)), key=lambda i: (-fused[i], i))
    return [candidates[i] for i in order]


def _positions(keys: dict[int, float]) -> dict[int, int]:
    """1-based rank by ascending key. Ties share the better position, so that
    equally good word matches leave the order to the other rankings rather than
    to the order the candidates arrived in."""
    positions: dict[int, int] = {}
    previous: float | None = None
    position = 0
    for n, i in enumerate(sorted(keys, key=keys.__getitem__), start=1):
        if keys[i] != previous:
            position, previous = n, keys[i]
        positions[i] = position
    return positions
