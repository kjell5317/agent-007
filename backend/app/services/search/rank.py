"""Relevance across sources (pure).

Results from different places carry scores that can't be compared — an RRF
fusion from Postgres, a Drive listing, a GitHub search. So the Enter search
re-scores every candidate the same way and fuses two rankings by rank
(Reciprocal Rank Fusion, like the local hybrid search):

  * semantic — cosine similarity between the query embedding and the
    candidate's (stored, or embedded from title + snippet);
  * lexical — how well each query word matches a word in the candidate's
    title and snippet, typo-tolerant via trigram overlap.

Ties keep the candidates' incoming order.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

RRF_K = 60
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass
class Candidate:
    hit: Any
    text: str
    semantic: float | None = None


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


def rank(query: str, candidates: list[Candidate], *, min_lexical: float = 0.3) -> list[Candidate]:
    """Candidates by fused relevance, best first. Lexical matches below
    `min_lexical` don't count as a match at all."""
    lexical = [lexical_score(query, c.text) for c in candidates]
    fused = [0.0] * len(candidates)
    by_semantic = sorted(
        (i for i, c in enumerate(candidates) if c.semantic is not None),
        key=lambda i: -candidates[i].semantic,
    )
    by_lexical = sorted((i for i in range(len(candidates)) if lexical[i] >= min_lexical),
                        key=lambda i: -lexical[i])
    for ranking in (by_semantic, by_lexical):
        for position, i in enumerate(ranking, start=1):
            fused[i] += 1.0 / (RRF_K + position)
    order = sorted(range(len(candidates)), key=lambda i: (-fused[i], i))
    return [candidates[i] for i in order]
