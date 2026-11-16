"""Write the body of a reply draft: a lookup loop over chat's read-only
search tools (the user's data and the web), then `write_reply`."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agent.helpers.llm import (
    LLMMessage,
    assistant_message,
    chat,
    tool_result_message,
    user_message,
)
from app.agent.chat.runner import Citations, read_only_tools, run_read_only_tool
from app.agent.helpers.text import now_iso
from app.agent.prompts.draft import DRAFT_REPLY_SYSTEM_PROMPT
from app.config import get_settings

log = logging.getLogger(__name__)

# Lookup rounds before the last step forces `write_reply`.
_MAX_STEPS = 6
# Gemini's thinking spends the output budget too; see services/task/split.py.
_MAX_TOKENS = 4096
_WRITE_REPLY_TOOL = {
    "name": "write_reply",
    "description": "The reply body, plus the facts the user must fill in before sending.",
    "parameters": {
        "type": "object",
        "properties": {
            "body": {"type": "string"},
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One entry per [[placeholder]] in the body; empty when none.",
            },
        },
        "required": ["body", "open_questions"],
    },
}


@dataclass(frozen=True)
class ThreadMessage:
    sender: str
    date: str
    body: str


def reply_prompt(
    *, task, brief: str, recipient: str, thread: list[ThreadMessage], examples: list[str],
    timezone: str,
) -> str:
    lines = [
        f"Current time: {now_iso(timezone)}",
        f"Task: {task.title}",
    ]
    if task.description:
        lines.append(f"Task description: {task.description}")
    lines += [f"Brief: {brief}", f"Reply goes to: {recipient}", "", "Thread, oldest first:"]
    for message in thread:
        lines += ["", f"--- From {message.sender} · {message.date} ---", message.body.strip()]
    if examples:
        lines += ["", "The user's own sent mail, for tone and length:"]
        for example in examples:
            lines += ["", "--- example ---", example.strip()]
    return "\n".join(lines)


async def write_reply(
    session: Session, *, task, brief: str, recipient: str, thread: list[ThreadMessage],
    examples: list[str],
) -> tuple[str, list[str]]:
    settings = get_settings()
    lookups = read_only_tools(session)
    cites = Citations()
    messages: list[LLMMessage] = [user_message(reply_prompt(
        task=task, brief=brief, recipient=recipient, thread=thread, examples=examples,
        timezone=settings.user_timezone,
    ))]
    # The same model as the lookup and chat (Gemini Flash): the tools are chat's.
    provider, model = settings.llm_target("chat")
    for step in range(_MAX_STEPS):
        last = step == _MAX_STEPS - 1
        resp = await chat(
            messages,
            settings,
            system_prompt=DRAFT_REPLY_SYSTEM_PROMPT,
            tools=[_WRITE_REPLY_TOOL] if last else [*lookups, _WRITE_REPLY_TOOL],
            force_tool="write_reply" if last else "any",
            provider=provider,
            model=model,
            thinking_level=settings.chat_thinking_level,
            max_tokens=_MAX_TOKENS,
            name="draft-reply",
        )
        reply = next((c for c in resp.tool_calls if c.name == "write_reply"), None)
        if reply is not None:
            body = str(reply.input.get("body") or "").strip()
            if not body:
                raise ValueError("The draft came back empty")
            questions = [str(q).strip() for q in reply.input.get("open_questions") or [] if str(q).strip()]
            return body, questions
        messages.append(assistant_message(resp))
        for call in resp.tool_calls:
            messages.append(tool_result_message(call, await run_read_only_tool(session, cites, call)))
    raise ValueError("The draft agent did not write a reply")
