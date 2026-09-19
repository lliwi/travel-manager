#!/bin/bash
# Wait for the datastores, bring the schema up to date, seed the baseline, then
# hand over to the command. Running migrations here rather than in a separate
# step keeps a fresh deployment to a single `docker compose up`.
set -e

echo "==> Esperando a PostgreSQL..."
until pg_isready -h postgres -p 5432 -U "${POSTGRES_USER:-travel}" -q; do
    sleep 1
done
echo "    PostgreSQL disponible."

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
echo "    Redis disponible."

echo "==> Esperando a MinIO..."
until curl -sf "${S3_ENDPOINT_URL:-http://minio:9000}/minio/health/live" >/dev/null; do
    sleep 1
done
echo "    MinIO disponible."

echo "==> Aplicando migraciones..."
if [ -d "migrations/versions" ] && [ -n "$(ls -A migrations/versions 2>/dev/null)" ]; then
    flask db upgrade
else
    # First run with no migration history: create the schema directly and
    # stamp it, so later migrations have a baseline to build on.
    echo "    Sin migraciones previas; creando el esquema inicial."
    flask init-db
fi

echo "==> Sembrando datos de referencia..."
flask seed || echo "    Aviso: no se pudo completar la siembra."

echo "==> Iniciando: $*"
exec "$@"
