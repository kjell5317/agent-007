"""System prompts. Each flow's prompt lives in its own module here so they
can be edited without churning the agent runner files.
"""

from app.agent.prompts.chat import CHAT_SYSTEM_PROMPT
from app.agent.prompts.new_input import NEW_INPUT_SYSTEM_PROMPT
from app.agent.prompts.thread import THREAD_FOLLOWUP_SYSTEM_PROMPT
from app.agent.prompts.manual import EXTRACT_FIELDS_SYSTEM_PROMPT

__all__ = [
    "CHAT_SYSTEM_PROMPT",
    "NEW_INPUT_SYSTEM_PROMPT",
    "THREAD_FOLLOWUP_SYSTEM_PROMPT",
    "EXTRACT_FIELDS_SYSTEM_PROMPT",
]
