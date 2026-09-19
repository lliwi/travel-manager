#!/bin/bash
# The worker waits for its dependencies but never runs migrations: the web
# container owns the schema, and two processes migrating concurrently is how a
# deployment corrupts itself.
set -e

echo "==> Esperando a PostgreSQL..."
until pg_isready -h postgres -p 5432 -U "${POSTGRES_USER:-travel}" -q; do
    sleep 1
done

echo "==> Esperando a Redis..."
until python - <<'PY'
import os, sys
import redis
try:
    redis.Redis.from_url(os.environ['REDIS_URL'], socket_timeout=2).ping()
except Exception:
    sys.exit(1)
PY
do
    sleep 1
done

echo "==> Iniciando: $*"
exec "$@"
