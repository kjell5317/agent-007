"""Agent runner: orchestrate one decision per raw input.

The package is split by flow:

  * `orchestrator` — picks the right flow per input
  * `thread_followup` — one-shot agent for replies on a known thread
  * `new_input` — multi-step agent for fresh inputs (dedup + create / not_task)
  * `extract` — field-extraction-only agent for manually promoted inputs

The two `llm` and `text` modules carry helpers shared across flows.
"""

from app.agent.runner.extract import extract_task_fields
from app.agent.runner.orchestrator import process_raw_input

__all__ = ["process_raw_input", "extract_task_fields"]
