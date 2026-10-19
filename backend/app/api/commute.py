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


@router.get("/flip", response_class=HTMLResponse)
async def confirm_flip(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    params = dict(request.query_params)
    try:
        flip_request = verify_flip(params, datetime.now(timezone.utc))
    except ValueError as exc:
        return _page(f"This link can't be used: {exc}.", status_code=403)
    try:
        question = await flip.describe_flip(session, flip_request)
    except LookupError:
        return _page(_GONE, status_code=404)
    return _page(question, form=params)


@router.post("/flip", response_class=HTMLResponse)
async def submit_flip(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    params = dict(parse_qsl((await request.body()).decode()))
    try:
        flip_request = verify_flip(params, datetime.now(timezone.utc))
    except ValueError as exc:
        return _page(f"This link can't be used: {exc}.", status_code=403)
    try:
        outcome = await flip.apply_flip(session, flip_request)
    except LookupError:
        return _page(_GONE, status_code=404)
    session.commit()
    return _page(outcome)


def _page(
    message: str,
    *,
    form: Mapping[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    button = ""
    if form is not None:
        fields = "".join(
            f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">'
            for key, value in form.items()
        )
        # Relative action: resolves to this same path behind any proxy prefix.
        button = (
            f'<form method="post" action="flip">{fields}'
            '<button type="submit">Switch</button></form>'
        )
    return HTMLResponse(
        _TEMPLATE.format(message=escape(message), button=button),
        status_code=status_code,
    )


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Commute</title>
<style>
  :root {{ color-scheme: light dark; --bg: #f5f5f4; --card: #fff; --fg: #1c1917; --accent: #2563eb; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #1c1917; --card: #292524; --fg: #f5f5f4; --accent: #60a5fa; }}
  }}
  body {{ margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: var(--bg); color: var(--fg); font: 18px/1.4 system-ui, sans-serif; }}
  main {{ margin: 16px; padding: 24px; max-width: 420px; width: 100%; box-sizing: border-box;
         background: var(--card); border-radius: 12px; text-align: center; }}
  p {{ margin: 0; }}
  button {{ margin-top: 20px; width: 100%; padding: 14px; border: 0; border-radius: 8px;
           background: var(--accent); color: #fff; font: inherit; font-weight: 600; }}
</style>
</head>
<body><main><p>{message}</p>{button}</main></body>
</html>
"""
