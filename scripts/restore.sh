#!/usr/bin/env bash
#
# Restauración completa: base de datos y documentos, del mismo momento.
#
# Termina comprobando la cadena de auditoría, y esa comprobación no es un
# adorno: si la cadena no cuadra después de restaurar, la copia estaba
# incompleta o alterada, y conviene saberlo antes de dejar entrar a nadie.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "${ROOT}/docker/docker-compose.yml")
ORIGEN=""
ASUMIR_SI=0
SOLO_BASE=0

usage() {
    cat <<'EOF'
Uso: scripts/restore.sh --from DIR [--yes] [--only-database]

  --from DIR        Directorio de copia (el que dejó backup.sh).
  --yes             No preguntar.
  --only-database   Restaura solo PostgreSQL. Para una prueba de restauración,
                    no para una recuperación real: los documentos quedarían
                    siendo los de ahora, no los de la copia.

Sobrescribe los datos actuales. Detiene web, worker y beat mientras dura.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --from) ORIGEN="$2"; shift 2 ;;
        --yes) ASUMIR_SI=1; shift ;;
        --only-database) SOLO_BASE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Opción desconocida: $1" >&2; usage; exit 2 ;;
    esac
done

[[ -n "${ORIGEN}" ]] || { echo "Falta --from." >&2; usage; exit 2; }
[[ -d "${ORIGEN}" ]] || { echo "No existe el directorio ${ORIGEN}." >&2; exit 1; }
[[ -f "${ORIGEN}/base_de_datos.dump" ]] || {
    echo "En ${ORIGEN} no hay base_de_datos.dump." >&2; exit 1; }

echo "==> Verificando la copia"
if [[ -f "${ORIGEN}/sumas.sha256" ]]; then
    ( cd "${ORIGEN}" && sha256sum --quiet -c sumas.sha256 ) || {
        echo "Las sumas de verificación no cuadran: la copia está alterada o incompleta." >&2
        exit 1
    }
    echo "    sumas correctas"
else
    echo "    (sin sumas; la copia no se puede verificar)" >&2
fi
cat "${ORIGEN}/manifiesto.txt" 2>/dev/null | sed 's/^/    /' || true

if [[ ${ASUMIR_SI} -ne 1 ]]; then
    read -r -p "Se sobrescribirán los datos actuales. ¿Continuar? [s/N] " respuesta
    [[ "${respuesta}" =~ ^[sSyY]$ ]] || { echo "Cancelado."; exit 1; }
fi

echo "==> Deteniendo la aplicación"
"${COMPOSE[@]}" stop web worker beat >/dev/null

arrancar() { "${COMPOSE[@]}" start web worker beat >/dev/null; }
trap arrancar EXIT

env_de() { "${COMPOSE[@]}" exec -T postgres printenv "$1" | tr -d '\r'; }
POSTGRES_USER="$(env_de POSTGRES_USER)"
POSTGRES_DB="$(env_de POSTGRES_DB)"

echo "==> Base de datos"
"${COMPOSE[@]}" exec -T postgres \
    pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --clean --if-exists \
    < "${ORIGEN}/base_de_datos.dump"

if [[ ${SOLO_BASE} -ne 1 ]]; then
    echo "==> Documentos"
    # «web» vuelve antes que el resto: es quien tiene el cliente S3, y los
    # objetos han de estar de vuelta antes de que el worker empiece a procesar
    # documentos que todavía no existirían en el almacén.
    "${COMPOSE[@]}" start web >/dev/null
    sleep 6
    "${COMPOSE[@]}" cp "${ORIGEN}/documentos.tar" web:/tmp/documentos.tar
    "${COMPOSE[@]}" exec -T web python - /tmp/documentos.tar \
        < "${ROOT}/scripts/restaurar_documentos.py" | sed 's/^/    /'
    "${COMPOSE[@]}" exec -T web rm -f /tmp/documentos.tar
fi

echo "==> Arrancando la aplicación"
arrancar
trap - EXIT
sleep 8

echo "==> Comprobando la cadena de auditoría"
"${COMPOSE[@]}" exec -T web flask verify-audit
echo "==> Restauración completada"
