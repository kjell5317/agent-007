"""Task labels — loaded from a TOML config file.

Every task gets a label assigned by the agent. Labels carry a short
description (shown to the agent so it can pick a fit) and a Google Calendar
event colorId (used when the task is mirrored to Calendar). A missing /
empty config disables labels entirely — the agent's `label` tool field
becomes optional and Calendar events stay un-colored.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

# Google Calendar event colors are IDs 1..11.
_VALID_COLOR_IDS = {str(i) for i in range(1, 12)}


@dataclass(frozen=True)
class Label:
    name: str
    description: str
    color: str  # Google Calendar event colorId, "1".."11"


@lru_cache
def load_labels() -> dict[str, Label]:
    """Return the configured labels keyed by name. Cached for process lifetime."""
    path = Path(get_settings().labels_config_path)
    if not path.is_absolute():
        # Resolve relative to repo root (two levels up from this file:
        # src/app/labels.py → src/app → src → repo root).
        path = Path(__file__).resolve().parents[2] / path
    if not path.is_file():
        log.warning("labels config not found at %s — labels disabled", path)
        return {}

    with path.open("rb") as f:
        data = tomllib.load(f)

    raw = data.get("labels") or {}
    out: dict[str, Label] = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            continue
        desc = str(body.get("description") or "").strip()
        color = str(body.get("color") or "").strip()
        if color and color not in _VALID_COLOR_IDS:
            log.warning("label %r has invalid color %r — using no color", name, color)
            color = ""
        out[name] = Label(name=name, description=desc, color=color)
    return out


def color_for(label_name: str | None) -> str | None:
    """Resolve a label's Google Calendar colorId, or None if unknown / unset."""
    if not label_name:
        return None
    label = load_labels().get(label_name)
    if label is None or not label.color:
        return None
    return label.color
