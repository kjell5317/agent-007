"""Cross-source poll dispatcher.

Single entry point that fans `poll_sources` out to the per-provider poll
modules under `app.services.input.<provider>.poll`. Add a new source by
implementing `poll(session, account_key)` in its provider folder and
adding it to the `_POLLERS` mapping below.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.orm import Session

from app.services.input.gmail import poll as gmail_poll
from app.services.input.slack import poll as slack_poll

log = logging.getLogger(__name__)

_Poller = Callable[[Session, str | None], Awaitable[dict]]

_POLLERS: dict[str, _Poller] = {
    "gmail": gmail_poll.poll,
    "slack": slack_poll.poll,
}

SUPPORTED_SOURCES: tuple[str, ...] = tuple(_POLLERS)


async def poll_sources(
    session: Session,
    *,
    source: str | None = None,
    account_key: str | None = None,
) -> dict:
    """Drive one or all sources through their poll routine. Raises ValueError
    for an unknown `source`. Called by both the `POST /sources/poll` endpoint
    and the background auto-poll job."""
    if source and source not in _POLLERS:
        raise ValueError(
            f"Unknown source {source!r}. Supported: {SUPPORTED_SOURCES}."
        )

    targets = [source] if source else list(SUPPORTED_SOURCES)

    aggregate: dict = {
        "fetched": 0,
        "agent_runs": 0,
        "tasks_created": 0,
        "skipped": 0,
        "errors": [],
        "per_source": {},
    }

    for name in targets:
        log.info("poll dispatch · source=%s account=%s", name, account_key or "(all)")
        result = await _POLLERS[name](session, account_key)
        aggregate["per_source"][name] = result
        for k in ("fetched", "agent_runs", "tasks_created", "skipped"):
            aggregate[k] += result.get(k, 0)
        aggregate["errors"].extend(result.get("errors", []))

    return aggregate
