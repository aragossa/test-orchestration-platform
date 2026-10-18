#!/bin/sh
# One image, two roles. `api` (default) applies migrations and serves HTTP;
# `worker` runs the orchestration loop. Alembic is idempotent, so an API
# restart (or a second replica racing on startup) is safe.
set -eu

role="${1:-api}"
if [ "$#" -gt 0 ]; then shift; fi

case "$role" in
  api)
    echo "[entrypoint] applying database migrations"
    alembic upgrade head
    echo "[entrypoint] starting API"
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' "$@"
    ;;
  worker)
    echo "[entrypoint] starting worker"
    exec python -m app.worker_service "$@"
    ;;
  *)
    echo "[entrypoint] unknown role '$role' (expected api or worker)" >&2
    exit 64
    ;;
esac
