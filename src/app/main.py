import asyncio
import logging
from contextlib import asynccontextmanager
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
from app import runtime_state
from app.api import auth as auth_router
from app.api import commute, inputs, labels, oauth, settings as settings_router, sources, tasks
from app.api.sources import poll_sources
from app.auth.middleware import AuthMiddleware
from app.config import get_settings
from app.db import SessionLocal
from app.services import task_creation_queue

AUTO_POLL_INTERVAL_S = 300

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


async def _auto_poll_loop() -> None:
    """Background job: poll every connected source every AUTO_POLL_INTERVAL_S
    seconds, gated by `runtime_state.auto_poll_enabled` (in-memory, resets on
    restart). One iteration's failure never kills the loop — we log and move on."""
    log = logging.getLogger("app.auto_poll")
    log.info("auto-poll loop started · interval=%ds", AUTO_POLL_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(AUTO_POLL_INTERVAL_S)
            if not runtime_state.auto_poll_enabled:
                log.debug("auto-poll skipped (disabled)")
                continue
            with SessionLocal() as session:
                summary = await poll_sources(session)
            log.info(
                "auto-poll done · fetched=%d created=%d skipped=%d errors=%d",
                summary["fetched"],
                summary["tasks_created"],
                summary["skipped"],
                len(summary["errors"]),
            )
        except asyncio.CancelledError:
            log.info("auto-poll loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("auto-poll iteration failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await task_creation_queue.start()
    task = asyncio.create_task(_auto_poll_loop(), name="auto-poll")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await task_creation_queue.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.log_level)
    app = FastAPI(
        title="Task Agent",
        version="0.1.0",
        debug=(settings.app_env == "dev"),
        lifespan=_lifespan,
    )

    app.include_router(auth_router.router)
    app.include_router(inputs.router)
    app.include_router(labels.router)
    app.include_router(tasks.router)
    app.include_router(oauth.router)
    app.include_router(sources.router)
    app.include_router(settings_router.router)
    app.include_router(commute.router)

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
