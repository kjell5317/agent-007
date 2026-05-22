# task-agent

Personal task-extraction agent over semi-structured inputs.

## Architecture

```md
[Sources]  →  [Ingestion]  →  [Queue]  →  [Agent]  →  [Storage]  →  [API]
 (TBD)        FastAPI         RQ +        Claude       Postgres     FastAPI
                              Redis      + tools     + pgvector     REST
```

Layout:

| Path                  | Purpose                                                       |
|-----------------------|---------------------------------------------------------------|
| `src/app/config.py`   | Settings (`pydantic-settings`)                                |
| `src/app/db.py`       | SQLAlchemy engine + session                                   |
| `src/app/models/`     | ORM models: `Task`, `RawInput`, `Feedback`, `OAuthToken`      |
| `src/app/schemas/`    | Pydantic DTOs for the API                                     |
| `src/app/api/`        | FastAPI routers: tasks, inputs, feedback, oauth               |
| `src/app/ingestion/`  | Generic source contract + registry (no source implemented)    |
| `src/app/auth/`       | Generic OAuth contract + registry + token encryption          |
| `src/app/agent/`      | Claude runner + tool schemas + system prompt                  |
| `src/app/queue/`      | RQ client + jobs + worker entry point                         |
| `src/app/storage/`    | DB-facing CRUD + vector search (stubbed)                      |
| `migrations/`         | Alembic                                                       |

## Status

Skeleton only — every behavior is a `TODO`. The structure is generic so a
specific source can be added without touching the agent, storage, or API.

## First-run setup

```bash
docker compose up -d                # Postgres (+pgvector) and Redis
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                # then set TOKEN_ENCRYPTION_KEY and ANTHROPIC_API_KEY
alembic revision --autogenerate -m "initial schema"   # after implementing models
alembic upgrade head
uvicorn app.main:app --reload
```

## Adding a source

1. Subclass `IngestionSource` under `src/app/ingestion/<name>.py`.
2. Decorate with `@register_source("name")`.
3. Import it from `app.main.create_app` so it registers at startup.
4. If the source uses OAuth, also add a provider under `src/app/auth/<name>.py`.
