"""Single-question Jev reads used outside triage."""

from __future__ import annotations

from typing import Any

from app.agent.helpers import jev
from app.agent.triage.policy import yes
from app.agent.triage.questions import split_question
from app.agent.triage.state import split_state
from app.config import get_settings


async def split_hint(task: Any) -> bool | None:
    """Whether a task holds independently schedulable pieces; None when Jev
    isn't acting or can't answer."""
    settings = get_settings()
    if not jev.acting(settings):
        return None
    try:
        result = await jev.ask(split_state(task), split_question(), settings, name="jev-split")
    except jev.JevUnavailable:
        return None
    return yes(result.answers["split"]) >= 0.5
