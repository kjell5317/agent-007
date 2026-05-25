"""Generic ingestion source contract.

Each concrete source (Gmail, Slack, manual, ...) subclasses `IngestionSource`
and registers itself via `@register_source("name")`. Sources are responsible
for two things:

1. Authenticating with their upstream provider (delegating to `app.auth`).
2. Translating their native payload into a `RawInputCreate` envelope.

Nothing source-specific lives outside the subclass — the rest of the app
only sees `RawInputCreate`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import ClassVar, TypeVar

from app.schemas.raw_input import RawInputCreate


class IngestionSource(ABC):
    """Abstract base class for any input source."""

    name: ClassVar[str] = ""

    @abstractmethod
    async def fetch(self) -> AsyncIterator[RawInputCreate]:
        """Pull new items from the source.

        Implementations may be polling-based (yield batches) or push-based
        (drain a webhook buffer). Either way, yield normalized envelopes.
        """
        raise NotImplementedError
        yield  # pragma: no cover - for typing

_REGISTRY: dict[str, type[IngestionSource]] = {}

# Preserve the decorated class's concrete type so type-checkers still see
# subclass-specific attributes (e.g. GmailSource.next_history_id) after the
# decorator runs. Without the TypeVar the return type would widen to
# `type[IngestionSource]` and those attributes look unknown to pyright.
_TSource = TypeVar("_TSource", bound=type[IngestionSource])


def register_source(name: str):
    """Class decorator to register a concrete source under a string key."""

    def _wrap(cls: _TSource) -> _TSource:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return _wrap


def get_source(name: str) -> type[IngestionSource]:
    if name not in _REGISTRY:
        raise KeyError(f"No ingestion source registered under {name!r}")
    return _REGISTRY[name]


def list_sources() -> list[str]:
    return sorted(_REGISTRY)
