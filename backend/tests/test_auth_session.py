from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.auth import middleware as auth_middleware


def test_authenticated_request_renews_session_cookie(monkeypatch):
    monkeypatch.setattr(
        auth_middleware,
        "get_settings",
        lambda: SimpleNamespace(auth_allowed_emails=["user@example.com"]),
    )
    app = FastAPI()

    @app.get("/auth/seed")
    def seed(request: Request):
        request.session["email"] = "user@example.com"
        return {"ok": True}

    @app.get("/private")
    def private():
        return {"ok": True}

    app.add_middleware(auth_middleware.AuthMiddleware)
    app.add_middleware(SessionMiddleware, secret_key="test-secret")

    with TestClient(app) as client:
        assert client.get("/auth/seed").status_code == 200
        response = client.get("/private")

    assert response.status_code == 200
    assert "session=" in response.headers["set-cookie"]
    assert "Max-Age=1209600" in response.headers["set-cookie"]
