#!/usr/bin/env bash
#
# Copia de seguridad completa: base de datos y documentos, juntos.
#
# Los dos almacenes se respaldan a la vez y en el mismo directorio a propósito.
# La base de datos guarda las claves de los objetos y el almacén guarda los
# objetos: una copia de uno solo, o dos copias de momentos distintos, produce
# una restauración en la que cada documento apunta a un fichero que no está.
#
# Si cualquiera de las dos mitades falla, no se deja nada detrás. Media copia
# que parece entera es peor que ninguna, porque nadie la vuelve a hacer.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "${ROOT}/docker/docker-compose.yml")
DESTINO="${ROOT}/backups"
CIFRAR=0

usage() {
    cat <<'EOF'
Uso: scripts/backup.sh [--output DIR] [--encrypt]

  --output DIR   Dónde dejar la copia. Por omisión, ./backups
  --encrypt      Cifra el resultado con gpg (pedirá una frase de paso).

Deja un directorio backup_AAAAMMDD_HHMMSS con la base de datos, los documentos
y un manifiesto con las sumas de verificación.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output) DESTINO="$2"; shift 2 ;;
        --encrypt) CIFRAR=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Opción desconocida: $1" >&2; usage; exit 2 ;;
    esac
done

FECHA="$(date +%Y%m%d_%H%M%S)"
TRABAJO="${DESTINO}/backup_${FECHA}"
mkdir -p "${TRABAJO}"

# Cualquier fallo se lleva por delante el directorio a medias.
limpiar_si_falla() {
    local codigo=$?
    if [[ ${codigo} -ne 0 ]]; then
        echo "==> La copia ha fallado; se descarta ${TRABAJO}" >&2
        rm -rf "${TRABAJO}"
    fi
    exit ${codigo}
}
trap limpiar_si_falla EXIT

env_de() { "${COMPOSE[@]}" exec -T postgres printenv "$1" | tr -d '\r'; }

echo "==> Base de datos"
POSTGRES_USER="$(env_de POSTGRES_USER)"
POSTGRES_DB="$(env_de POSTGRES_DB)"
"${COMPOSE[@]}" exec -T postgres \
    pg_dump -U "${POSTGRES_USER}" -Fc "${POSTGRES_DB}" > "${TRABAJO}/base_de_datos.dump"

if [[ ! -s "${TRABAJO}/base_de_datos.dump" ]]; then
    echo "El volcado de la base de datos está vacío." >&2
    exit 1
fi

echo "==> Documentos"
# Se bajan con el cliente S3 que la aplicación ya lleva dentro, en vez de con
# una imagen aparte: no hace falta descargar nada, no hay credenciales
# circulando por la línea de órdenes, y funciona igual contra MinIO que contra
# cualquier S3. Llegan como un tar por la salida estándar, así que tampoco hay
# que montar el directorio de copia dentro del contenedor.
# El script va por la entrada estándar en lugar de por una ruta: así no
# depende de que el directorio esté montado dentro del contenedor. Escribe a un
# fichero y se saca después con «cat»: crear la aplicación imprime una línea de
# registro en la salida estándar, y una línea de registro delante de un tar
# hace que deje de ser un tar.
"${COMPOSE[@]}" exec -T web python - /tmp/documentos.tar \
    < "${ROOT}/scripts/volcar_documentos.py" | sed 's/^/    /'
"${COMPOSE[@]}" exec -T web cat /tmp/documentos.tar > "${TRABAJO}/documentos.tar"
"${COMPOSE[@]}" exec -T web rm -f /tmp/documentos.tar

if [[ ! -s "${TRABAJO}/documentos.tar" ]]; then
    echo "No se pudo leer el almacén de documentos." >&2
    exit 1
fi

echo "==> Manifiesto"
{
    echo "fecha_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "base_de_datos: ${POSTGRES_DB}"
    echo "documentos: $(tar -tf "${TRABAJO}/documentos.tar" | wc -l) objetos"
    echo "git: $(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo desconocido)"
} > "${TRABAJO}/manifiesto.txt"

# Las sumas son lo que permite saber, al restaurar, si la copia llegó entera.
( cd "${TRABAJO}" && find . -type f ! -name sumas.sha256 -print0 \
  | sort -z | xargs -0 sha256sum > sumas.sha256 )

if [[ ${CIFRAR} -eq 1 ]]; then
    echo "==> Cifrado"
    tar -C "${DESTINO}" -czf "${TRABAJO}.tar.gz" "backup_${FECHA}"
    gpg --symmetric --cipher-algo AES256 "${TRABAJO}.tar.gz"
    rm -rf "${TRABAJO}" "${TRABAJO}.tar.gz"
    echo "==> Listo: ${TRABAJO}.tar.gz.gpg"
else
    echo "==> Listo: ${TRABAJO}"
fi

trap - EXIT
