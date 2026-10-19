"""Flip-link landing pages for commute legs.

Exempt from the login check — the signed query is the auth. A GET only
renders a confirm page (link previews and prefetchers open links); the
switch happens on the page's POST.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from html import escape
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.services.commute import flip
from app.services.commute.flip_link import verify_flip

router = APIRouter(prefix="/commute", tags=["commute"])

_GONE = "This leg no longer exists."
_UNUSABLE = "This link can't be used"


@router.get("/flip", response_class=HTMLResponse)
async def confirm_flip(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    params = dict(request.query_params)
    try:
        flip_request = verify_flip(params, datetime.now(timezone.utc))
    except ValueError as exc:
        return _message_page(_UNUSABLE, f"{exc}.", error=True, status_code=403)
    try:
        preview = await flip.describe_flip(session, flip_request)
    except LookupError:
        return _message_page(_GONE, "It was re-planned or removed.", error=True, status_code=404)
    return _confirm_page(preview, params)


@router.post("/flip", response_class=HTMLResponse)
async def submit_flip(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    params = dict(parse_qsl((await request.body()).decode()))
    try:
        flip_request = verify_flip(params, datetime.now(timezone.utc))
    except ValueError as exc:
        return _message_page(_UNUSABLE, f"{exc}.", error=True, status_code=403)
    try:
        outcome = await flip.apply_flip(session, flip_request)
    except LookupError:
        return _message_page(_GONE, "It was re-planned or removed.", error=True, status_code=404)
    session.commit()
    return _message_page(outcome, "You can close this tab.")


def _confirm_page(preview: flip.FlipPreview, form: Mapping[str, str]) -> HTMLResponse:
    target = flip.MODE_LABELS[preview.target]
    current = flip.MODE_LABELS.get(preview.current, preview.current)
    modes = (
        f'<div class="modes">{_mode_chip(preview.current, current)}'
        f'<span class="arrow" aria-hidden="true">→</span>'
        f'{_mode_chip(preview.target, target, selected=preview.current != preview.target)}</div>'
    )
    destination = (
        f'<p class="muted">{escape(preview.destination)}</p>'
        if preview.destination and preview.destination != preview.place
        else ""
    )
    if preview.current == preview.target:
        action = f'<p class="note">Already {escape(target.lower())} — nothing to switch.</p>'
    else:
        fields = "".join(
            f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
            for key, value in form.items()
        )
        # Relative action: resolves to this same path behind any proxy prefix.
        # Disabling on submit keeps a double tap from posting twice.
        action = (
            f'<form method="post" action="flip" '
            f"onsubmit=\"this.querySelector('button').disabled = true\">{fields}"
            f'<button type="submit">Switch to {escape(target.lower())}</button></form>'
        )
    body = (
        f'<p class="eyebrow">Commute · {escape(preview.when)}</p>'
        f"{modes}<h1>to {escape(preview.place)}</h1>{destination}{action}"
    )
    return HTMLResponse(_TEMPLATE.format(body=body))


def _mode_chip(mode: str, label: str, *, selected: bool = False) -> str:
    icon = flip.MODE_ICONS.get(mode, "")
    css = "mode selected" if selected else "mode"
    return f'<span class="{css}"><span aria-hidden="true">{icon}</span>{escape(label)}</span>'


def _message_page(
    title: str,
    detail: str,
    *,
    error: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    eyebrow = '<p class="eyebrow error">Commute</p>' if error else '<p class="eyebrow">Commute</p>'
    body = f'{eyebrow}<h1>{escape(title)}</h1><p class="muted">{escape(detail)}</p>'
    return HTMLResponse(_TEMPLATE.format(body=body), status_code=status_code)


# Colors mirror the app's theme tokens in frontend/src/index.css.
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Commute</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: hsl(210 20% 98%); --card: hsl(0 0% 100%); --fg: hsl(222 47% 11%);
    --muted: hsl(215 16% 47%); --border: hsl(214 32% 91%); --secondary: hsl(210 40% 96%);
    --primary: hsl(221 83% 53%); --primary-fg: hsl(210 40% 98%); --destructive: hsl(0 72% 51%);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: hsl(225 14% 9%); --card: hsl(225 13% 13%); --fg: hsl(210 20% 96%);
      --muted: hsl(215 15% 68%); --border: hsl(224 11% 24%); --secondary: hsl(224 12% 18%);
      --primary: hsl(217 91% 66%); --primary-fg: hsl(222 47% 11%); --destructive: hsl(0 72% 57%);
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100dvh; display: flex; align-items: center; justify-content: center;
    padding: 16px; background: var(--bg); color: var(--fg);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  main {{
    width: 100%; max-width: 400px; padding: 28px 24px 24px; text-align: center;
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    box-shadow: 0 10px 30px rgb(0 0 0 / 0.06);
  }}
  .eyebrow {{
    margin: 0 0 16px; font-size: 12px; font-weight: 600; letter-spacing: 0.06em;
    text-transform: uppercase; color: var(--muted);
  }}
  .eyebrow.error {{ color: var(--destructive); }}
  .modes {{ display: flex; align-items: center; justify-content: center; gap: 10px; margin-bottom: 16px; }}
  .mode {{
    display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 14px;
    border-radius: 999px; background: var(--secondary); color: var(--muted);
    font-size: 14px; font-weight: 500;
  }}
  .mode.selected {{ background: var(--primary); color: var(--primary-fg); }}
  .arrow {{ color: var(--muted); }}
  h1 {{ margin: 0; font-size: 22px; font-weight: 600; line-height: 1.3; overflow-wrap: anywhere; }}
  .muted {{ margin: 6px 0 0; font-size: 14px; color: var(--muted); overflow-wrap: anywhere; }}
  .note {{ margin: 24px 0 0; font-size: 14px; color: var(--muted); }}
  form {{ margin: 24px 0 0; }}
  button {{
    width: 100%; height: 48px; border: 0; border-radius: 10px; cursor: pointer;
    background: var(--primary); color: var(--primary-fg); font: inherit; font-weight: 600;
    transition: opacity 0.15s;
  }}
  button:hover {{ opacity: 0.9; }}
  button:disabled {{ opacity: 0.5; cursor: default; }}
  button:focus-visible {{ outline: 2px solid var(--primary); outline-offset: 2px; }}
</style>
</head>
<body><main>{body}</main></body>
</html>
"""
