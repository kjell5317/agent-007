"""Text cleanup shared by the source preprocessors. Pure functions only."""

from __future__ import annotations


def collapse_whitespace(text: str) -> str:
    """Trim trailing whitespace per line and collapse 3+ blank lines to one."""
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if not line:
            blanks += 1
            if blanks <= 1:
                out.append("")
        else:
            blanks = 0
            out.append(line)
    return "\n".join(out).strip()
