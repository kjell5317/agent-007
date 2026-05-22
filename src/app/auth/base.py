"""Generic OAuth provider contract.

Concrete providers (Google, Slack, GitHub, Notion, ...) subclass `OAuthProvider`
and register themselves via `@register_provider("name")`. The routes in
`app.api.oauth` use the registry to dispatch authorization and callback flows
without knowing anything provider-specific.

Implementations should delegate the actual OAuth dance to a library
(e.g. Authlib) rather than rolling their own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scopes: list[str]
    extra: dict


class OAuthProvider(ABC):
    """Abstract base class for an OAuth provider."""

    name: ClassVar[str] = ""

    @abstractmethod
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        """Return the URL to redirect the user to for consent."""

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        """Exchange an authorization code for tokens."""

    @abstractmethod
    async def refresh(self, refresh_token: str) -> TokenBundle:
        """Refresh an access token."""

    @abstractmethod
    async def identify(self, access_token: str) -> str:
        """Return a stable per-account key (email, user id, workspace id, ...)."""

    # TODO: token revocation hook
    # TODO: scope negotiation per source need (least-privilege per integration)


_REGISTRY: dict[str, type[OAuthProvider]] = {}


def register_provider(name: str):
    def _wrap(cls: type[OAuthProvider]) -> type[OAuthProvider]:
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return _wrap


def get_provider(name: str) -> type[OAuthProvider]:
    if name not in _REGISTRY:
        raise KeyError(f"No OAuth provider registered under {name!r}")
    return _REGISTRY[name]


def list_providers() -> list[str]:
    return sorted(_REGISTRY)
