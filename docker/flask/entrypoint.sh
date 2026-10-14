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

# «Disponible» no es «acepta la contraseña»: pg_isready no se autentica, y la
# comprobación de salud de PostgreSQL tampoco. Se prueba aquí, antes de
# migrar, para que el fallo diga qué pasa en vez de una traza repetida cada
# pocos segundos mientras el contenedor se reinicia.
if ! python - <<'PY'
import sys

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from app.config import _database_url

try:
    create_engine(_database_url()).connect().close()
except OperationalError as exc:
    if 'password authentication failed' not in str(exc):
        raise
    sys.exit(1)
PY
then
    cat >&2 <<'MSG'

  ✗ PostgreSQL rechaza la contraseña de docker/.env.

    PostgreSQL fija la contraseña solo la primera vez, al crear su volumen
    de datos. Si POSTGRES_PASSWORD cambió después -- regenerada, editada, o
    traída de otra máquina junto con el código --, el volumen conserva la
    antigua y no hace caso de la nueva.

    Para que acepte la de docker/.env sin perder datos, en el servidor:

      source docker/.env
      docker exec travelmanager_postgres psql -U "${POSTGRES_USER:-travel}" -d "${POSTGRES_DB:-travel_manager}" -c "ALTER USER ${POSTGRES_USER:-travel} WITH PASSWORD '${POSTGRES_PASSWORD}';"

    Desde dentro del contenedor no se pide contraseña, así que funciona aunque
    no se sepa cuál era la antigua.

MSG
    # Sin la pausa, Docker reinicia el contenedor en bucle y el mensaje se
    # pierde entre cientos de repeticiones.
    sleep 60
    exit 1
fi

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

echo "==> Esperando al almacén de documentos..."
until curl -sf "${S3_ENDPOINT_URL:-http://s3:9000}/healthz" >/dev/null; do
    sleep 1
done
echo "    Almacén de documentos disponible."

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
