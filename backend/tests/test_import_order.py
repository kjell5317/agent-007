"""Every entry point must import on its own, in a fresh interpreter.

The suite imports modules in one fixed order, so a circular import that only
breaks when e.g. `app.main` is loaded first (as uvicorn does) slips past every
other test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ENTRY_POINTS = [
    "app.main",
    "app.cron",
    "app.events",
    "app.agent",
    "app.services.search",
    "app.services.calendar",
    "app.services.attribute_classifier",
    "app.services.task.queue",
    "app.scripts.jev_replay",
    "app.scripts.backfill_task_attributes",
]


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_entry_point_imports_first(module):
    env = {**os.environ, "DATABASE_URL": "postgresql+psycopg://t:t@localhost/t"}
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
