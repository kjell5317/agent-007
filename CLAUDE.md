# CLAUDE.md

Context for Claude Code sessions in this repo. Read this first.

## Goal

Personal task-extraction agent. Takes semi-structured input from multiple
sources (Gmail today; Slack / manual / others later) and decides:

1. Is this a task for me?
2. If yes — extract title, estimated duration, due date, location, source links.
3. Avoid creating duplicates.
4. Capture user feedback so future extractions improve.

Personal use only, but keep the design open so any new source plugs in
without touching the agent, storage, or API layers.

## Architecture

```md
[Sources] → [Ingestion] → [Queue] → [Agent] → [Storage] → [API]
            FastAPI       RQ +     Haystack  Postgres    FastAPI
                          Redis    + tools  + pgvector   REST
```

Stack: Python 3.11+, FastAPI, SQLAlchemy 2.x, Postgres + pgvector,
RQ + Redis, Haystack LLM orchestration, Authlib + httpx for OAuth.

### Layering rules

- Every source normalizes into a `RawInputCreate` envelope before crossing
  the ingestion boundary. Nothing source-specific exists in the agent,
  storage, or API layers.
- Sources and OAuth providers register themselves via decorators
  (`@register_source`, `@register_provider`). To add one, write the file
  and add a side-effect import in [src/app/main.py](src/app/main.py).
- DB access lives in `app.storage`. Both the API layer and the agent
  runner call the same functions there — no logic in the routers.
- Tools the agent can call are declared in
  [src/app/agent/tools.py](src/app/agent/tools.py) with explicit JSON
  schemas. The runner dispatches by name into `app.storage`.

### Directory map

| Path                          | Responsibility                                  |
|-------------------------------|-------------------------------------------------|
| `src/app/config.py`           | Settings (`pydantic-settings`)                  |
| `src/app/db.py`               | SQLAlchemy engine + session                     |
| `src/app/models/`             | ORM models (`Task`, `RawInput`, `Feedback`, …)  |
| `src/app/schemas/`            | Pydantic DTOs                                   |
| `src/app/api/`                | FastAPI routers — thin, no business logic       |
| `src/app/ingestion/`          | Source contract + per-source subpackages        |
| `src/app/auth/`               | OAuth contract + per-provider + token crypto    |
| `src/app/agent/`              | Prompt, tool schemas, runner                    |
| `src/app/storage/`            | DB-facing CRUD + pgvector similarity            |
| `src/app/queue/`              | RQ client, jobs, worker entry point             |
| `migrations/`                 | Alembic                                         |

## Extension points

### Adding a source

1. Create `src/app/ingestion/<name>/source.py` (or single file for simple
   sources). Subclass `IngestionSource`, decorate with `@register_source("name")`.
2. If preprocessing is non-trivial (parsing, cleaning, dedup keying),
   put it in `src/app/ingestion/<name>/preprocess.py` as pure functions —
   no I/O, no DB — so it's unit-testable from fixture dicts.
3. If the source needs OAuth, also add an `OAuthProvider` (see below).
4. Add the side-effect import to `src/app/main.py`.

### Adding an OAuth provider

1. Create `src/app/auth/<name>.py`. Subclass `OAuthProvider`, decorate
   with `@register_provider("name")`.
2. Implement: `authorize_url`, `exchange_code`, `refresh`, `identify`.
3. Add provider config fields to `app.config.Settings` and `.env.example`.
4. Add the side-effect import to `src/app/main.py`.

## Best practices

### Code style

- Type hints throughout. Use modern syntax: `list[str]`, `str | None`,
  `dict[str, Any]` — not `List`, `Optional`, `Dict`.
- Default to writing no comments. Only document the WHY when it's
  non-obvious (a constraint, a quirk, a workaround). Never document
  the WHAT — naming should carry that.
- No docstrings on every function. Module-level docstrings are fine
  when the module's role isn't obvious from its location.
- Prefer pure functions for any logic that can be expressed as one
  (preprocessing, parsing, formatting). Reserve I/O for the edges.
- Keep routers thin: validate → call storage/agent → return. Business
  logic lives one layer down.

### Dependencies

- Minimal deps. Prefer stdlib + httpx + one or two focused libraries
  over heavyweight SDKs (e.g. Gmail client uses raw httpx, not the
  full `google-api-python-client`).
- Pin major versions in `pyproject.toml`. Don't add a dep until at
  least two places would need it.

### Security

- OAuth tokens are stored as Fernet ciphertext in `oauth_tokens`.
  Decrypt only at point of use (see `app.auth.crypto`).
- Scope OAuth requests to least-privilege (e.g. Gmail uses `.readonly`).
- Verify webhook signatures before parsing payloads — every source's
  `handle_webhook` should do its own signature check first.

### Building features

- Build a walking skeleton end-to-end before deepening any layer.
  Manual `POST /inputs` → sync agent call → DB → `GET /tasks` is
  the right v1 shape; queue and webhooks come after.
- Don't add error handling, fallbacks, or validation for scenarios
  that can't happen. Trust internal code. Only validate at boundaries
  (HTTP input, external APIs).
- Don't introduce abstractions for hypothetical future sources/providers.
  Three similar lines is better than a premature base class — the
  registry pattern is the only abstraction we've committed to.

### TODO discipline

- Skeleton code is marked with `TODO` and raises `NotImplementedError`.
  Treat TODOs as a deliberate punch list, not aspirational comments.
- When implementing a TODO, delete the comment.
- New gaps surfaced during implementation get new TODOs in the same
  commit, so the punch list stays current.

## Out of scope (for now)

- Multi-user / auth on the API itself (personal use, localhost).
- Real-time push (Gmail Pub/Sub, Slack Socket Mode) — polling is fine.
- Fine-tuning. Feedback feeds few-shot examples in the prompt instead.
- Self-hosted vector DB. pgvector stays in Postgres alongside tasks.

## Conventions for Claude

- When asked to plan, explain tradeoffs in 2–3 sentences with a
  recommendation. Don't implement until the user agrees.
- When implementing, follow the layering rules above. If a change
  needs to cross layers, flag it.
- Don't create README/docs files unless explicitly asked.
- Don't generate commit messages or commits unless asked.
