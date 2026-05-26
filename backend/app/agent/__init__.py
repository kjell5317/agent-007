"""Task-extraction agent.

Public entry points:

  * `process_raw_input` — orchestrate the right flow (thread follow-up vs.
    auto-decide vs. new-input agent) for a single raw input.
  * `extract_task_fields` — field-extraction-only agent for manually-promoted
    inputs (no dedup, just `create_task`).
"""

from app.agent.manual.runner import extract_task_fields
from app.agent.orchestrator import process_raw_input

__all__ = ["process_raw_input", "extract_task_fields"]
