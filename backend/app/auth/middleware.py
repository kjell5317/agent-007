"""HTTP middleware enforcing the email allowlist.

Disabled when AUTH_ALLOWED_EMAILS is empty (local dev / tests). When enabled:

  - Browser requests (`Accept: text/html`) → 302 to /auth/login
  - API clients (everything else)         → 401 JSON

Exempts /auth/* (the login flow itself) and /health (so external healthchecks
work without a session).

Requires SessionMiddleware to be added AFTER this one — middleware order is
last-added-first-executed, so a later add wraps an earlier one. Result:
SessionMiddleware populates request.session before AuthMiddleware reads it.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

log = logging.getLogger(__name__)

_EXEMPT_PREFIXES = ("/auth/", "/notifications/")
_EXEMPT_PATHS = {"/health"}


def _is_exempt_request(method: str, path: str) -> bool:
    if path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
        return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.auth_allowed_emails:
            return await call_next(request)

        path = request.url.path
        if _is_exempt_request(request.method, path):
            return await call_next(request)

        email = request.session.get("email") if hasattr(request, "session") else None
        if email and email.lower() in settings.auth_allowed_emails:
            return await call_next(request)

        accepts_html = "text/html" in request.headers.get("accept", "")
        if accepts_html:
            return RedirectResponse("/auth/login", status_code=302)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
