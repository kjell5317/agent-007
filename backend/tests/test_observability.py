"""Langfuse observability seam: masking, and that every helper is a safe no-op
when tracing is disabled (the default in tests — no credentials)."""

from __future__ import annotations

from app import observability as obs


def test_mask_redacts_secrets_keeps_content():
    assert obs._mask(data="my key is sk-ABCDEFGH12345678 ok") == "my key is [REDACTED] ok"
    assert obs._mask(data="Bearer abc.def-123") == "[REDACTED]"
    assert obs._mask(data="ntn_" + "A" * 24) == "[REDACTED]"
    assert obs._mask(data="ghp_" + "b" * 30) == "[REDACTED]"
    # Ordinary content is preserved untouched.
    assert obs._mask(data="Buy milk before Friday") == "Buy milk before Friday"


def test_mask_recurses_into_containers():
    out = obs._mask(data={"a": "pk-lf-abcdef123456", "b": ["fine", "sk-ABCDEFGH12345678"]})
    assert out == {"a": "[REDACTED]", "b": ["fine", "[REDACTED]"]}


def test_disabled_helpers_are_noops():
    # No credentials configured in the test env → tracing disabled.
    assert obs.enabled() is False

    with obs.root_span("x", session_id="s", tags=["t"]) as span:
        span.update(output="anything")  # must not raise
        with obs.generation("g", model="m", input=[{"role": "user"}]) as gen:
            gen.update(output="o", usage_details={"input": 1}, metadata={"k": "v"})

    # Trace-level + lifecycle helpers are also no-ops.
    obs.set_trace_io(input={"q": 1}, output="a")
    obs.flush()
    obs.shutdown()
