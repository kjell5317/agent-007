#!/usr/bin/env bash
# Local dev loop: start Postgres in Docker, run Alembic, then uvicorn (reload).
# Frontend runs separately:  cd frontend && npm run dev
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon not reachable — start Docker / OrbStack first." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo ".venv missing — run scripts/setup.sh first." >&2
  exit 1
fi

# `.env.dev` is the dev-only overlay (OAuth redirect URIs at :5173, etc.).
# Pydantic loads it after `.env`, so its keys win. Seed it from `.env` the
# first time so the override file exists and is easy to find.
if [[ ! -f .env.dev ]]; then
  if [[ -f .env ]]; then
    cp .env .env.dev
    echo "→ seeded .env.dev from .env — edit for local overrides"
  else
    echo ".env missing — run scripts/setup.sh first." >&2
    exit 1
  fi
else
  echo "→ using .env + .env.dev (overrides)"
fi

echo "→ postgres up…"
docker compose up -d postgres

echo "→ waiting for postgres…"
until docker compose exec -T postgres pg_isready -U taskagent -d taskagent >/dev/null 2>&1; do
  sleep 0.5
done

echo "→ alembic upgrade head…"
uv run alembic upgrade head

echo "→ uvicorn (http://127.0.0.1:8001) — Ctrl-C to stop"
exec uv run uvicorn app.main:app \
  --reload \
  --reload-dir backend \
  --host 127.0.0.1 \
  --port 8001
