import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Side-effect imports register OAuth providers and ingestion sources via their
# @register_* decorators. Add a new provider/source = add a new import here.
import app.auth.google  # noqa: F401
import app.auth.slack  # noqa: F401
import app.ingestion.gmail  # noqa: F401
import app.ingestion.slack  # noqa: F401
from app.api import auth as auth_router
from app.api import inputs, oauth, sources, tasks
from app.auth.middleware import AuthMiddleware
from app.config import get_settings

_STATIC_DIR = Path(__file__).parent / "static"
_ASSETS_DIR = _STATIC_DIR / "assets"


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s · %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    lvl = level.upper()
    for name in ("app", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logger = logging.getLogger(name)
        logger.setLevel(lvl)
        logger.handlers = [handler]
        logger.propagate = False


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)
    app = FastAPI(
        title="Task Agent",
        version="0.1.0",
        debug=(settings.app_env == "dev"),
    )

    app.include_router(auth_router.router)
    app.include_router(inputs.router)
    app.include_router(tasks.router)
    app.include_router(oauth.router)
    app.include_router(sources.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    # Vite builds into static/. assets/ contains the hashed JS/CSS chunks;
    # index.html is the SPA entry. Order matters: API routes are registered
    # above, so they win over the catch-all index handler.
    if _ASSETS_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    # Middleware order matters: add_middleware wraps the existing stack, so
    # the LAST added is the OUTERMOST (runs first on the way in). We need
    # SessionMiddleware to populate request.session BEFORE AuthMiddleware
    # reads it — so add AuthMiddleware first, SessionMiddleware second.
    if settings.auth_allowed_emails:
        if not settings.session_secret:
            raise RuntimeError(
                "SESSION_SECRET is required when AUTH_ALLOWED_EMAILS is set. "
                "Generate: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        app.add_middleware(AuthMiddleware)
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            same_site="lax",
            https_only=(settings.app_env != "dev"),
        )

    return app


app = create_app()
