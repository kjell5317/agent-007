"""Signed links that switch a commute leg's mode from its calendar event.

The signature is the only auth on `/commute/flip` (it's exempt from the
login check), so links are signed with the notification action secret and
expire an hour after the leg ends — a finished leg is folded into the stored
position and deleted soon after, so a later flip has nothing to act on. Links
are deterministic: the planner diffs descriptions to decide whether to
patch, so a nonce or timestamp would rewrite every leg on every replan.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from app.config import get_settings
from app.services.ntfy import action_secret

if TYPE_CHECKING:
    from app.services.commute.flip import FlipRequest

FLIP_PATH = "/commute/flip"
# A finished leg is folded into the stored position and deleted by the hourly
# cleanup, so a link only needs to outlive its leg until then.
LINK_LIFETIME = timedelta(hours=1)

_OTHER_MODE = {"bicycling": "transit", "transit": "bicycling"}
_SIGNED_FIELDS = ("origin", "dest", "depart", "mode", "expires")
# Same secret as the ntfy action buttons — the prefix keeps a signature from
# one scheme from ever validating in the other.
_SIGNATURE_SCOPE = "commute-flip"


def flip_url(
    origin_anchor: str,
    dest_anchor: str,
    depart: datetime,
    arrive: datetime,
    current_mode: str,
) -> str | None:
    target = _OTHER_MODE.get(current_mode)
    if target is None:
        return None
    settings = get_settings()
    secret = action_secret(settings)
    if not secret:
        return None
    params = {
        "origin": origin_anchor,
        "dest": dest_anchor,
        "depart": str(_epoch(depart)),
        "mode": target,
        "expires": str(_epoch(arrive + LINK_LIFETIME)),
    }
    params["sig"] = _signature(params, secret)
    return f"{settings.public_url}{FLIP_PATH}?{urlencode(params)}"


def verify_flip(params: Mapping[str, str], now: datetime) -> FlipRequest:
    # flip.py re-plans through the planner, which renders these links.
    from app.services.commute.flip import FlipRequest

    try:
        fields = {key: params[key] for key in _SIGNED_FIELDS}
        signature = params["sig"]
    except KeyError as exc:
        raise ValueError(f"missing parameter {exc.args[0]!r}") from None
    secret = action_secret(get_settings())
    expected = _signature(fields, secret)
    if not secret or not hmac.compare_digest(signature.encode(), expected.encode()):
        raise ValueError("invalid signature")
    if now.timestamp() > int(fields["expires"]):
        raise ValueError("link expired")
    return FlipRequest(
        origin_anchor=fields["origin"],
        dest_anchor=fields["dest"],
        depart=datetime.fromtimestamp(int(fields["depart"]), timezone.utc),
        mode=fields["mode"],
    )


def _signature(fields: Mapping[str, str], secret: str) -> str:
    message = "\n".join([_SIGNATURE_SCOPE, *(fields[key] for key in _SIGNED_FIELDS)])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())
