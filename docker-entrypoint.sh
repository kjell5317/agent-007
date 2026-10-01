#!/bin/sh
# Run DB migrations before starting the app. alembic.ini's sqlalchemy.url
# points at localhost, but the env.py we ship reads DATABASE_URL — so the
# container picks up the postgres service hostname from compose.
set -e

echo "running alembic upgrade heads…"
# `heads` also works when two migration branches are present in an older image.
# A later merge revision converges both branches to one head.
alembic upgrade heads

echo "starting: $*"
exec "$@"
