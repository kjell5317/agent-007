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
from typing import ClassVar, TypeVar


@dataclass
class OAuthAuthorization:
    url: str
    context: dict | None = None


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

    async def authorize(self, state: str, redirect_uri: str) -> OAuthAuthorization:
        """Build an authorization redirect and optional transient callback context."""
        return OAuthAuthorization(url=self.authorize_url(state, redirect_uri))

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str) -> TokenBundle:
        """Exchange an authorization code for tokens."""

    async def exchange_code_with_context(
        self, code: str, redirect_uri: str, context: dict | None
    ) -> TokenBundle:
        """Exchange a code using transient state saved during authorization."""
        return await self.exchange_code(code, redirect_uri)

    @abstractmethod
    async def refresh(self, refresh_token: str) -> TokenBundle:
        """Refresh an access token."""

    async def refresh_with_context(
        self, refresh_token: str, context: dict | None
    ) -> TokenBundle:
        """Refresh an access token using persisted provider metadata when needed."""
        return await self.refresh(refresh_token)

    @abstractmethod
    async def identify(self, access_token: str) -> str:
        """Return a stable per-account key (email, user id, workspace id, ...)."""

    async def identify_with_context(self, access_token: str, context: dict | None) -> str:
        """Return a stable account key using provider metadata when needed."""
        return await self.identify(access_token)

    # TODO: token revocation hook
    # TODO: scope negotiation per source need (least-privilege per integration)


_REGISTRY: dict[str, type[OAuthProvider]] = {}

# See base.py in ingestion for the rationale — preserve concrete class type
# through the decorator so subclass-specific __init__ signatures survive.
_TProvider = TypeVar("_TProvider", bound=type[OAuthProvider])


def register_provider(name: str):
    def _wrap(cls: _TProvider) -> _TProvider:
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
