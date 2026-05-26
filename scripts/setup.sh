#!/usr/bin/env bash
# First-time local dev setup: Python venv via uv, editable install,
# frontend deps, .env scaffold. Re-run any time pyproject.toml or
# frontend/package.json changes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — install: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

echo "→ uv venv (.venv)…"
uv venv --python 3.12

echo "→ python deps (editable)…"
uv pip install -e ".[dev]"

echo "→ frontend deps…"
(cd frontend && npm install)

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo
  echo "wrote .env from .env.example — fill in the secrets before scripts/dev.sh"
fi

echo
echo "setup complete. Next:"
echo "  scripts/dev.sh                  # postgres + migrations + backend"
echo "  cd frontend && npm run dev      # frontend (separate terminal)"
