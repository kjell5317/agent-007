from fastapi import FastAPI

from app.api import feedback, inputs, oauth, tasks
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

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    # TODO: register ingestion sources on startup (importing app.ingestion.<source>)
    # TODO: register OAuth providers on startup (importing app.auth.<provider>)
    # TODO: structured logging + request id middleware

    return app


app = create_app()
