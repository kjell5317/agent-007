from fastapi import FastAPI

# Side-effect imports register OAuth providers and ingestion sources via their
# @register_* decorators. Add a new provider/source = add a new import here.
import app.auth.google  # noqa: F401
import app.ingestion.gmail  # noqa: F401
from app.api import feedback, inputs, oauth, sources, tasks
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Task Agent",
        version="0.1.0",
        debug=(settings.app_env == "dev"),
    )

    app.include_router(inputs.router)
    app.include_router(tasks.router)
    app.include_router(feedback.router)
    app.include_router(oauth.router)
    app.include_router(sources.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    # TODO: structured logging + request id middleware

    return app


app = create_app()
