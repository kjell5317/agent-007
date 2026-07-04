# Task Agent

A personal task-extraction agent. It watches your message sources (Gmail,
Slack), uses a Haystack-backed LLM to decide whether each message is actually
a task **for you**, and — when it is — extracts a structured task, files it without
creating duplicates, and mirrors it to your calendar.

It's built for a single user (me), but the architecture is deliberately
source-agnostic: adding a new input source or OAuth provider is a single
self-registering file, with no changes to the agent, storage, or API.

> **Heads up:** this is a personal project shared for reference. There's no
> multi-tenant auth, and it expects you to bring your own API keys. See
> [Configuration](#configuration).

## What it does

- **Multi-source ingestion** — pulls messages from Gmail and Slack over OAuth,
  normalizing every source into one internal envelope before anything
  source-specific can leak downstream.
- **Task vs. noise, decided by an LLM** — the agent reads an input plus
  similar past inputs and decides `task` / `not a task` / `duplicate`. For real
  tasks it extracts a title, label, due date, time estimate, how much of it is
  AI-doable (0–1), and a confidence score.
- **Semantic de-duplication** — inputs and tasks are embedded (Gemini) and
  compared with pgvector cosine similarity, so the same request arriving twice
  doesn't become two tasks. High-similarity inputs can reuse a past decision
  without spending an LLM call.
- **Calendar mirroring** — extracted tasks become Google Calendar events,
  color-coded by label.
- **Commute planning** — events with a location get weather-aware travel time
  blocked out, and rescheduling when an edit creates an overlap.
- **Gamification** — a running points score (shown in the topbar) earned by
  completing tasks, adjustable by hand or from Home Assistant.
- **Push notifications** — errors and updates are pushed via a Home Assistant
  notify service (nothing fails silently).
- **Web UI** — a React/Vite single-page app for the inbox and tasks.

## Architecture

```text
[Sources]  →  [Ingestion]  →  [Agent]      →  [Storage]    →  [API + SPA]
 Gmail         normalize       Haystack LLM     Postgres        FastAPI
 Slack         to RawInput     + tools          + pgvector      React/Vite
                               (in-proc queue)
```

- Every source normalizes into a `RawInput` envelope at the ingestion boundary.
- Sources and OAuth providers self-register via `@register_source` /
  `@register_provider` decorators; a side-effect import in
  [`main.py`](backend/app/main.py) is all that wires one in.
- All DB access lives in `app.db.clients`; routers stay thin and the agent
  runner and API call the same storage functions.
- Tools the agent may call are declared with explicit JSON schemas in
  [`agent/tools/`](backend/app/agent/tools/); the runner dispatches by name.
- A background asyncio worker processes inputs; a small cron loop handles
  polling, calendar discovery, and the weather/commute refresh.

## Tech stack

Python 3.12 · FastAPI · SQLAlchemy 2 · Postgres + pgvector · Haystack-backed
LLM orchestration (Anthropic initially) · Gemini embeddings · Authlib + httpx for OAuth ·
React + Vite + Tailwind · Docker Compose + Caddy · Terraform (Hetzner +
Cloudflare) for deployment.

## Quick start (Docker)

```bash
git clone https://github.com/kjell5317/agent-007.git
cd agent-007

cp .env.example .env                         # then fill in the secrets below
cp config/labels.toml.example config/labels.toml

docker compose up -d --build                 # Postgres + app + Caddy
```

Migrations run automatically on startup. The app is then reachable through
Caddy (`https://localhost` by default). The API's OpenAPI docs live at
`/docs`.

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Node.js.

```bash
scripts/setup.sh                # uv venv, Python + frontend deps, .env scaffold
scripts/dev.sh                  # Postgres in Docker, migrations, backend (reload)
cd frontend && npm run dev      # frontend, separate terminal
```

The backend serves on `http://127.0.0.1:8001`; the Vite dev server proxies to
it. `scripts/dev.sh` seeds a `.env.dev` overlay (loaded after `.env`) for
dev-only overrides like OAuth redirect URIs.

## Configuration

All secrets come from environment variables — see
[`.env.example`](.env.example) for the full list and inline notes. The
essentials:

| Variable | Purpose |
| --- | --- |
| `LLM_PROVIDER` / `LLM_MODEL` | Haystack LLM backend selection; `anthropic` is currently supported |
| `ANTHROPIC_API_KEY` | Anthropic API access when `LLM_PROVIDER=anthropic` |
| `GEMINI_API_KEY` | Gemini embeddings for de-duplication and note retrieval |
| `TOKEN_ENCRYPTION_KEY` | Fernet key — OAuth tokens are stored encrypted at rest |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google OAuth for SSO, Gmail, Calendar, and Google Fit sleep access |
| `SLACK_APPS` | Per-workspace Slack OAuth apps (JSON) |
| `HOME_ADDRESS` | Origin/destination for commute planning |
| `HOME_ASSISTANT_URL` / `_TOKEN` | Push notifications (optional) |
| `AUTH_ALLOWED_EMAILS` / `SESSION_SECRET` | Google-SSO email allowlist for the UI |

Runtime config that isn't secret lives in [`config/`](config/) — task
`labels.toml`. It's personal and git-ignored; copy the `*.example` template
and edit. Points scoring is controlled by `POINTS_TASK_DONE_FACTOR` in `.env`.
See [`config/README.md`](config/README.md).

## Project structure

```text
backend/app/
  api/            FastAPI routers (thin: validate → storage/agent → return)
  agent/          Haystack LLM runners, prompts, tool schemas
  auth/           OAuth contract + per-provider + token encryption
  services/
    input/        ingestion: source contract + Gmail/Slack subpackages
    calendar/     Google Calendar sync
    health/       isolated Google Fit sleep read handler
    commute/      travel-time + weather planning
    plan/         scheduling
  db/             SQLAlchemy models, clients (CRUD + vector search), schemas
  config/         pydantic-settings
  cron.py         background polling / refresh loop
migrations/       Alembic
frontend/         React + Vite + Tailwind SPA
terraform/        Hetzner + Cloudflare deployment
```

## Extending it

**Add an input source:** create
`backend/app/services/input/<name>/source.py`, subclass `IngestionSource`,
decorate with `@register_source("name")`, and add a side-effect import to
`main.py`. Keep any non-trivial parsing in a pure `preprocess.py` so it's
unit-testable from fixtures.

**Add an OAuth provider:** create `backend/app/auth/<name>.py`, subclass
`OAuthProvider`, decorate with `@register_provider("name")`, implement
`authorize_url` / `exchange_code` / `refresh` / `identify`, add config fields,
and import it in `main.py`.

## Deployment

[`terraform/`](terraform/) provisions a Hetzner Cloud server with a Cloudflare
DNS record, clones the repo, and runs the Docker Compose stack behind Caddy
(automatic Let's Encrypt TLS when `APP_DOMAIN` is a real domain). You supply
`.env` and the `config/` files out of band, the same way they're kept out of
git.

## License

No license is granted — this is a personal project published for reference.
If you'd like to reuse part of it, open an issue.
