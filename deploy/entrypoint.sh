#!/bin/sh
# Apply migrations, then start the API + worker. Alembic is idempotent, so a
# restart (or a second replica racing on startup) is safe.
set -eu

echo "[entrypoint] applying database migrations"
alembic upgrade head

echo "[entrypoint] starting application"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' "$@"
