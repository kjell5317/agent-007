"""Jev client: TypeSafe's System One endpoint over raw httpx.

One call sends a `state` plus named questions (Choice / Score / Noul) and gets
one calibrated answer per question back. Jev writes no text and does no date
arithmetic, so callers keep generation in the LLM and calendar math in code.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app import observability as obs
from app.config import Settings

log = logging.getLogger(__name__)

# 429 (rate limit) and 529 (overloaded) are documented as retry-after-a-pause.
_RETRY_STATUSES = frozenset({429, 529, 502, 503, 504})
_RETRY_DELAY_S = 0.5


class JevUnavailable(RuntimeError):
    """Jev could not answer; callers fall back to the LLM path."""


@dataclass(frozen=True)
class Choice:
    instructions: str
    criteria: Mapping[str, str | None]


@dataclass(frozen=True)
class Score:
    instructions: str
    levels: Sequence[str]


@dataclass(frozen=True)
class Noul:
    instructions: str
    true: str | None = None
    false: str | None = None


Question = Choice | Score | Noul


@dataclass(frozen=True)
class Answer:
    """`value` is the chosen option (Choice), the most probable level index as
    a string (Score) or "yes"/"no" (Noul); `p` is that option's probability."""

    value: str
    p: float
    probabilities: dict[str, float] = field(default_factory=dict)

    def p_of(self, option: str) -> float:
        return float(self.probabilities.get(option, 0.0))


@dataclass(frozen=True)
class JevResult:
    model: str
    answers: dict[str, Answer]
    input_tokens: int
    latency_ms: int

    def trace(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "answers": {
                name: {"value": a.value, "p": round(a.p, 4)} for name, a in self.answers.items()
            },
        }


def configured(settings: Settings) -> bool:
    return bool(settings.typesafe_api_key.strip()) and settings.jev_mode != "off"


def acting(settings: Settings) -> bool:
    return bool(settings.typesafe_api_key.strip()) and settings.jev_mode == "on"


async def ask(
    state: Any,
    questions: Mapping[str, Question],
    settings: Settings,
    *,
    name: str = "jev",
) -> JevResult:
    payload = {
        "model": settings.jev_model,
        "state": state,
        "questions": {key: question_json(q) for key, q in questions.items()},
    }
    headers = {"Authorization": f"Bearer {settings.typesafe_api_key.strip()}"}
    url = f"{settings.jev_base_url.rstrip('/')}/v1/systemone"
    started = time.monotonic()
    with obs.generation(
        name,
        model=settings.jev_model,
        input={"state": state, "questions": payload["questions"]},
        metadata={"provider": "typesafe"},
    ) as gen:
        body = await _post(url, payload, headers, settings.jev_timeout_seconds)
        result = parse_response(
            body, questions, latency_ms=round((time.monotonic() - started) * 1000)
        )
        gen.update(
            output=result.trace()["answers"],
            usage_details={"input": result.input_tokens},
            metadata={"model": result.model, "latency_ms": result.latency_ms},
        )
    return result


async def _post(url: str, payload: dict, headers: dict[str, str], timeout: float) -> dict:
    last_error = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(2):
            try:
                resp = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in _RETRY_STATUSES:
                    break
            if attempt == 0:
                await asyncio.sleep(_RETRY_DELAY_S)
    log.warning("jev · request failed %s", last_error)
    raise JevUnavailable(last_error)


def question_json(question: Question) -> dict[str, Any]:
    if isinstance(question, Choice):
        return {
            "type": "choice",
            "instructions": question.instructions,
            "criteria": dict(question.criteria),
        }
    if isinstance(question, Score):
        return {
            "type": "score",
            "instructions": question.instructions,
            "criteria": list(question.levels),
        }
    out: dict[str, Any] = {"type": "noul", "instructions": question.instructions}
    if question.true or question.false:
        out["criteria"] = {"true": question.true, "false": question.false}
    return out


def parse_response(
    body: Mapping[str, Any], questions: Mapping[str, Question], *, latency_ms: int
) -> JevResult:
    raw_answers = body.get("answers") if isinstance(body, Mapping) else None
    if not isinstance(raw_answers, Mapping):
        raise JevUnavailable("response carried no answers")
    answers: dict[str, Answer] = {}
    for name, question in questions.items():
        raw = raw_answers.get(name)
        if not isinstance(raw, Mapping):
            raise JevUnavailable(f"response missing answer {name!r}")
        answers[name] = _answer(question, raw)
    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    return JevResult(
        model=str(body.get("model") or ""),
        answers=answers,
        input_tokens=int(usage.get("input_tokens") or 0),
        latency_ms=latency_ms,
    )


def _answer(question: Question, raw: Mapping[str, Any]) -> Answer:
    if isinstance(question, Noul):
        p_yes = float(raw.get("noul") or 0.0)
        probabilities = {"yes": p_yes, "no": 1.0 - p_yes}
        value = "yes" if p_yes >= 0.5 else "no"
        return Answer(value=value, p=probabilities[value], probabilities=probabilities)
    probabilities = {
        str(k): float(v)
        for k, v in (raw.get("probabilities") or {}).items()
        if isinstance(v, (int, float))
    }
    if not probabilities:
        raise JevUnavailable("answer carried no probabilities")
    # A Score's `score` is the expected level; the docs warn against reading
    # magnitudes off it, so callers get the most probable level instead.
    value = max(probabilities, key=probabilities.get)
    return Answer(value=value, p=probabilities[value], probabilities=probabilities)
