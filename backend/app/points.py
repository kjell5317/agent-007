"""Points actions + task-completion factor — loaded from a YAML config file.

Mirrors `app.labels`: a hand-edited config drives a UI surface. Here it's the
Points page. Each section (Sport / Nutrition / Other) lists actions; an action
has a name, a factor, and an optional unit. Submitting an action adds
`factor × quantity` points (quantity = the number the user enters, or 1 when
the action has no unit). `task_done_factor` is points-per-estimated-minute
awarded when a task is completed. A missing/empty config disables everything —
the page renders empty sections and no task-completion points are awarded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import get_settings

log = logging.getLogger(__name__)

# (config table key, display title) in render order.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("sport", "Sport"),
    ("nutrition", "Nutrition"),
    ("other", "Other"),
)


@dataclass(frozen=True)
class PointAction:
    name: str
    factor: float
    unit: str | None


@dataclass(frozen=True)
class PointsConfig:
    task_done_factor: float
    sections: dict[str, list[PointAction]]  # keyed by section table key


@lru_cache
def load_points_config() -> PointsConfig:
    """Return the parsed points config. Cached for process lifetime."""
    path = _resolve(Path(get_settings().points_config_path))
    if path is None:
        log.warning("points config not found — Points page disabled")
        return PointsConfig(0.0, {key: [] for key, _ in SECTIONS})

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        log.warning("points config root is not a mapping — Points page disabled")
        return PointsConfig(0.0, {key: [] for key, _ in SECTIONS})

    return PointsConfig(
        task_done_factor=_coerce_factor(data.get("task_done_factor")),
        sections={key: _parse_actions(key, data.get(key)) for key, _ in SECTIONS},
    )


def find_action(section: str, name: str) -> PointAction | None:
    """Look up a configured action by section key and name, or None."""
    return next(
        (a for a in load_points_config().sections.get(section, []) if a.name == name),
        None,
    )


def _parse_actions(section: str, raw: object) -> list[PointAction]:
    if not isinstance(raw, list):
        return []
    out: list[PointAction] = []
    for body in raw:
        if not isinstance(body, dict):
            continue
        name = str(body.get("name") or "").strip()
        factor = _coerce_factor(body.get("factor"))
        # Negative factors are allowed (an action that costs points, e.g.
        # "Sweets"); only a missing name or a zero/unparseable factor is invalid.
        if not name or factor == 0:
            log.warning("points action in %r missing name or valid factor — skipped", section)
            continue
        unit = str(body.get("unit") or "").strip() or None
        out.append(PointAction(name=name, factor=factor, unit=unit))
    return out


def _coerce_factor(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _resolve(raw: Path) -> Path | None:
    if raw.is_absolute():
        return raw if raw.is_file() else None
    # CWD first (Docker), then the source-tree repo root (local dev) — same
    # resolution `app.labels` uses for its config file.
    candidates = [Path.cwd() / raw, Path(__file__).resolve().parents[2] / raw]
    return next((p for p in candidates if p.is_file()), None)
