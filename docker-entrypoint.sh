#!/bin/sh
# Run DB migrations before starting the app. alembic.ini's sqlalchemy.url
# points at localhost, but the env.py we ship reads DATABASE_URL — so the
# container picks up the postgres service hostname from compose.
set -e

echo "running alembic upgrade head…"
alembic upgrade head

echo "starting: $*"
exec "$@"
