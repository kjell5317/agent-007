"""Langfuse tracing — the single seam that touches the Langfuse SDK.

Tracing is off unless `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set;
every helper here is a safe no-op in that case, so call sites stay unconditional.

Why manual instrumentation rather than the Haystack auto-instrumentor: this app
calls `AnthropicChatGenerator.run_async()` directly, not through a Haystack
`Pipeline`, so the pipeline-oriented OpenInference instrumentor wouldn't see the
calls. Instrumenting our own LLM boundary (`agent/helpers/llm.py`) captures every
call — with model, tokens, and I/O — across all agent flows.

Langfuse is imported lazily inside the functions so importing this module has no
side effects and adds no startup cost when tracing is disabled.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any

from app.config import Settings

log = logging.getLogger(__name__)

_enabled = False

# Redact anything that looks like a credential from traced payloads. The task /
# email / calendar content IS the point of the trace (single-user, the user's own
# data), so content is not masked — only secrets that might ride along in a string.
_SECRET_RE = re.compile(
    r"(sk-lf-[A-Za-z0-9\-_]{6,}|pk-lf-[A-Za-z0-9\-_]{6,}|sk-[A-Za-z0-9\-_]{16,}"
    r"|gh[posru]_[A-Za-z0-9]{20,}|ntn_[A-Za-z0-9]{20,}"
    r"|Bearer\s+[A-Za-z0-9\-_.=]+)"
)


def _mask(*, data: Any, **_: Any) -> Any:
    if isinstance(data, str):
        return _SECRET_RE.sub("[REDACTED]", data)
    if isinstance(data, dict):
        return {k: _mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_mask(data=v) for v in data]
    return data


class _NoopObservation:
    """Stand-in yielded by the context managers when tracing is disabled, so
    call sites can always call `.update(...)`."""

    def update(self, **_: Any) -> None:  # noqa: D401
        return None


_NOOP = _NoopObservation()


def init_langfuse(settings: Settings) -> None:
    """Initialize the Langfuse singleton. Idempotent; a no-op without credentials."""
    global _enabled
    if _enabled:
        return
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        log.info("Langfuse tracing disabled (no LANGFUSE_* credentials)")
        return
    from langfuse import Langfuse

    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        environment=settings.app_env,
        release=settings.langfuse_release or None,
        mask=_mask,
    )
    _enabled = True
    log.info("Langfuse tracing enabled · host=%s env=%s", settings.langfuse_host, settings.app_env)


def enabled() -> bool:
    return _enabled


@contextlib.contextmanager
def root_span(name: str, *, session_id: str | None = None, tags: list[str] | None = None):
    """Open a root observation for one agent run and stamp trace-level attributes.

    Groups every nested observation (LLM generations, sub-steps) under one named,
    filterable trace. No-op when tracing is disabled."""
    if not _enabled:
        yield _NOOP
        return
    from langfuse import get_client, propagate_attributes

    with get_client().start_as_current_observation(as_type="span", name=name) as span:
        with propagate_attributes(session_id=session_id, tags=tags, trace_name=name):
            yield span


@contextlib.contextmanager
def generation(
    name: str,
    *,
    model: str | None = None,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
):
    """Open a generation observation around an LLM call. No-op when disabled."""
    if not _enabled:
        yield _NOOP
        return
    from langfuse import get_client

    with get_client().start_as_current_observation(
        as_type="generation", name=name, model=model, input=input, metadata=metadata
    ) as gen:
        yield gen


def set_trace_io(*, input: Any = None, output: Any = None) -> None:
    if _enabled:
        from langfuse import get_client

        get_client().set_current_trace_io(input=input, output=output)


def flush() -> None:
    if _enabled:
        from langfuse import get_client

        get_client().flush()


def shutdown() -> None:
    if _enabled:
        from langfuse import get_client

        get_client().shutdown()
