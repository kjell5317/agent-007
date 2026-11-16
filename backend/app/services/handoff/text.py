"""Pure formatting for what hand-offs write into a task description."""

from __future__ import annotations


def append_section(description: str | None, heading: str, body: str) -> str:
    section = f"**{heading}**\n\n{body.strip()}"
    current = (description or "").rstrip()
    return f"{current}\n\n{section}" if current else section


def sources_markdown(sources: list[tuple[str, str]]) -> str:
    return "Sources: " + ", ".join(f"[{title}]({url})" for title, url in sources)
