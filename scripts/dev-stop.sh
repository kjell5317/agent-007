#!/usr/bin/env bash
# Stop the dev Postgres container. Data volume is preserved — to wipe it
# entirely, run `docker compose down -v` manually.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

docker compose stop postgres
