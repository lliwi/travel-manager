#!/usr/bin/env bash
#
# Travel Manager — arranque y parada del sistema.
#
#   ./start.sh --dev            arrancar en desarrollo (recarga automática)
#   ./start.sh --dev --stop     detener el entorno de desarrollo
#   ./start.sh --prod           arrancar en producción (Gunicorn + Nginx)
#   ./start.sh --prod --stop    detener el entorno de producción
#
# Al arrancar se aplican las actualizaciones que hagan falta: se reconstruyen
# las imágenes si el código o las dependencias han cambiado, se ejecutan las
# migraciones pendientes y se siembran los ajustes o catálogos nuevos. Todo
# ello es idempotente, así que ejecutarlo dos veces seguidas no hace daño.
#
set -euo pipefail

# ---------------------------------------------------------------------
# Presentación
# ---------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; DIM='\033[2m'; BOLD='\033[1m'; NC='\033[0m'

header() { echo -e "\n${BOLD}${BLUE}==> $1${NC}"; }
ok()     { echo -e "  ${GREEN}✓${NC} $1"; }
warn()   { echo -e "  ${YELLOW}!${NC} $1"; }
fail()   { echo -e "  ${RED}✗${NC} $1" >&2; }
info()   { echo -e "    ${DIM}$1${NC}"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT}/docker/.env"
COMPOSE_PROD="${ROOT}/docker/docker-compose.yml"
COMPOSE_DEV="${ROOT}/docker/docker-compose.dev.yml"
STATE_FILE="${ROOT}/docker/.entorno"

usage() {
    cat <<'EOF'
Uso: ./start.sh [--dev | --prod] [--stop] [opciones]

Entornos
  --dev          Desarrollo: recarga automática, registro legible, sin Nginx
                 ni antivirus. La aplicación se publica en el puerto 5000.
  --prod         Producción: Gunicorn tras Nginx, registro JSON, ClamAV activo.

Acciones
  (ninguna)      Arrancar, aplicando las actualizaciones necesarias.
  --stop         Detener los servicios, conservando los datos.

Opciones
  --build        Forzar la reconstrucción de las imágenes.
  --no-build     No reconstruir aunque haya cambios.
  --proxy [URL]  Construir a través de este proxy, solo esta vez. Sin opción
                 ya se usa BUILD_PROXY de docker/.env si está definido, o
                 HTTPS_PROXY del entorno. Solo afecta a la construcción: los
                 contenedores no lo heredan.
  --no-migrate   No aplicar migraciones ni siembra.
  --logs         Seguir el registro al terminar de arrancar.
  --status       Mostrar el estado y salir.
  -h, --help     Esta ayuda.

Ejemplos
  ./start.sh --dev                 arrancar en desarrollo
  ./start.sh --dev --logs          arrancar y seguir el registro
  ./start.sh --prod --build        arrancar en producción reconstruyendo
  ./start.sh --dev --build --proxy http://proxy.corp.local:3128
                                   reconstruir detrás de un proxy corporativo
  ./start.sh --prod --stop         detener producción
EOF
}

# ---------------------------------------------------------------------
# Argumentos
# ---------------------------------------------------------------------
ENTORNO=""
ACCION="start"
FORZAR_BUILD=""
MIGRAR="si"
SEGUIR_LOGS=""
PROXY_BUILD=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dev|--desarrollo)      ENTORNO="dev" ;;
        --prod|--produccion)     ENTORNO="prod" ;;
        --stop|--parar|--detener) ACCION="stop" ;;
        --status|--estado)       ACCION="status" ;;
        --build)                 FORZAR_BUILD="si" ;;
        --proxy)
            # La URL detrás es opcional: «--proxy https://...» la toma, y
            # «--proxy» a secas la busca en la configuración. Se comprueba que
            # lo siguiente no sea otra opción para no tragarse un «--build».
            if [[ $# -gt 1 && "$2" != -* ]]; then
                PROXY_BUILD="$2"
                shift
            else
                PROXY_BUILD="auto"
            fi
            ;;
        --no-build)              FORZAR_BUILD="no" ;;
        --no-migrate|--sin-migrar) MIGRAR="no" ;;
        --logs|--registro)       SEGUIR_LOGS="si" ;;
        -h|--help|--ayuda)       usage; exit 0 ;;
        *) fail "Opción desconocida: $1"; echo; usage; exit 2 ;;
    esac
    shift
done

# Sin entorno explícito se reutiliza el último usado, para que un `--stop`
# suelto detenga lo que efectivamente está en marcha.
if [[ -z "${ENTORNO}" ]]; then
    if [[ -f "${STATE_FILE}" ]]; then
        ENTORNO="$(cat "${STATE_FILE}")"
        info "Entorno no indicado; se usa el último: --${ENTORNO}"
    else
        fail "Indique el entorno: --dev o --prod"
        echo
        usage
        exit 2
    fi
fi

if [[ "${ENTORNO}" == "dev" ]]; then
    COMPOSE_ARGS=(-f "${COMPOSE_PROD}" -f "${COMPOSE_DEV}")
    ETIQUETA="desarrollo"
else
    COMPOSE_ARGS=(-f "${COMPOSE_PROD}")
    ETIQUETA="producción"
fi

compose() { docker compose "${COMPOSE_ARGS[@]}" --env-file "${ENV_FILE}" "$@"; }

# ---------------------------------------------------------------------
# Comprobaciones previas
# ---------------------------------------------------------------------
comprobar_requisitos() {
    command -v docker >/dev/null 2>&1 || { fail "Docker no está instalado."; exit 1; }
    docker compose version >/dev/null 2>&1 || { fail "Se requiere Docker Compose v2."; exit 1; }
    docker info >/dev/null 2>&1 || { fail "El demonio de Docker no responde."; exit 1; }

    if [[ ! -f "${ENV_FILE}" ]]; then
        fail "No existe docker/.env."
        info "Ejecute ./setup.sh para crearlo y generar los secretos."
        exit 1
    fi

    # Un valor de ejemplo olvidado en producción es un fallo de seguridad, no
    # una molestia: la aplicación se negaría a arrancar de todos modos.
    if [[ "${ENTORNO}" == "prod" ]]; then
        local pendientes
        pendientes="$(grep -cE '^[A-Z_]+=cambie-' "${ENV_FILE}" || true)"
        if [[ "${pendientes}" -gt 0 ]]; then
            fail "Quedan ${pendientes} valores sin configurar en docker/.env."
            grep -E '^[A-Z_]+=cambie-' "${ENV_FILE}" | cut -d= -f1 | sed 's/^/      /'
            info "Ejecute ./setup.sh para generarlos."
            exit 1
        fi
    fi
}

# ---------------------------------------------------------------------
# Acciones
# ---------------------------------------------------------------------
mostrar_estado() {
    header "Estado (${ETIQUETA})"
    if [[ -z "$(compose ps -q 2>/dev/null)" ]]; then
        info "No hay servicios en marcha."
        return
    fi
    compose ps --format 'table {{.Name}}\t{{.Service}}\t{{.Status}}'
}

detener() {
    header "Deteniendo Travel Manager (${ETIQUETA})"
    # «-a»: también los parados y los que se reinician en bucle. Sin él, con
    # la aplicación caída «ps» no veía nada, no se ejecutaba «down», y el
    # contenedor roto, su red y su estado seguían ahí para el siguiente
    # arranque.
    if [[ -z "$(compose ps -a -q 2>/dev/null)" ]]; then
        info "No había servicios."
        return
    fi
    compose down --remove-orphans
    ok "Servicios detenidos. Los datos se conservan en los volúmenes."
    info "Para borrarlos también: docker compose -f docker/docker-compose.yml down -v"
}

necesita_build() {
    # Reconstruir si se pide, si falta la imagen, o si algo que entra en ella
    # es más reciente que la propia imagen.
    [[ "${FORZAR_BUILD}" == "si" ]] && return 0
    [[ "${FORZAR_BUILD}" == "no" ]] && return 1

    local imagen creada
    imagen="$(compose config --images 2>/dev/null | grep -m1 'travel-manager' || true)"
    if [[ -z "${imagen}" ]] || ! docker image inspect "${imagen}" >/dev/null 2>&1; then
        return 0
    fi

    creada="$(docker image inspect -f '{{.Created}}' "${imagen}" 2>/dev/null || echo '')"
    [[ -z "${creada}" ]] && return 0

    local marca
    marca="$(mktemp)"
    # `date -d` es GNU; en macOS se recurre a reconstruir por si acaso.
    touch -d "${creada}" "${marca}" 2>/dev/null || { rm -f "${marca}"; return 0; }

    local cambios
    # Las migraciones y el conjunto de evaluación también van dentro de la
    # imagen: una migración nueva sin reconstruir no se aplica nunca.
    #
    # Fuera lo que los propios scripts escriben en docker/ al arrancar -- la
    # redirección, los secretos, el entorno, los certificados --, que no entra
    # en ninguna imagen: con ellos, cada arranque se veía «con cambios» y
    # reconstruía todo, a través del proxy, sin haber tocado una línea.
    cambios="$(find "${ROOT}/app" "${ROOT}/migrations" "${ROOT}/data/evaluacion" \
        "${ROOT}/requirements.txt" "${ROOT}/constraints.txt" "${ROOT}/docker" \
        -newer "${marca}" \
        ! -path "${ROOT}/docker/.env*" \
        ! -path "${ROOT}/docker/.entorno" \
        ! -path "${ROOT}/docker/nginx/tls_redirect.conf" \
        ! -path "${ROOT}/docker/nginx/certs*" \
        -print -quit 2>/dev/null || true)"
    rm -f "${marca}"

    [[ -n "${cambios}" ]]
}

# El proxy de construcción vive en scripts/lib/proxy.sh, compartido con
# setup.sh: los dos tienen que tomar la misma decisión, y antes uno lo buscaba
# en docker/.env y el otro en el .env de la raíz.
# shellcheck source=scripts/lib/proxy.sh
source "${ROOT}/scripts/lib/proxy.sh"

# HTTP o HTTPS: en scripts/lib/tls.sh, compartido con setup.sh para que los
# dos lleguen a la misma conclusión.
# shellcheck source=scripts/lib/tls.sh
source "${ROOT}/scripts/lib/tls.sh"

construir() {
    # «--proxy URL» manda para esta ejecución; «--proxy» a secas, o nada, usa
    # BUILD_PROXY de docker/.env, que se aplica siempre que esté definido: si
    # está escrito es porque esta máquina no sale sin él, y exigir además la
    # opción en cada arranque hacía fallar la reconstrucción automática.
    if [[ -n "${PROXY_BUILD}" && "${PROXY_BUILD}" != "auto" ]]; then
        PROXY_FORZADO="${PROXY_BUILD}" construir_imagenes
    else
        construir_imagenes
    fi
}

arrancar() {
    header "Arrancando Travel Manager (${ETIQUETA})"

    if necesita_build; then
        info "Se han detectado cambios; reconstruyendo las imágenes."
        construir
        ok "Imágenes actualizadas."
    elif [[ -n "${PROXY_BUILD}" ]]; then
        # Pedir un proxy y que no se construya nada deja a quien lo pidió sin
        # saber si se usó. Se dice, y se sigue.
        info "No hay cambios que reconstruir; el proxy no se ha usado."
    else
        ok "Las imágenes están al día."
    fi

    # Antes de levantar: Nginx lee el fragmento al arrancar, así que decidirlo
    # después obligaría a reiniciarlo.
    if [[ "${ENTORNO}" == "prod" ]]; then
        configurar_tls
    fi

    compose up -d --remove-orphans
    ok "Servicios arrancados."

    # Nginx resuelve «web» al arrancar y se queda con esa dirección. Si esta
    # vuelta ha recreado el contenedor de la aplicación —lo hace en cuanto
    # cambia la imagen— la dirección cambia y Nginx sigue llamando a la
    # anterior: todo responde «healthy» y el navegador ve 502, que es el modo
    # de fallo más caro de diagnosticar porque nada parece estar roto.
    compose restart nginx >/dev/null 2>&1 || true

    header "Esperando a que la aplicación responda"
    local intentos=0
    until docker exec travelmanager_web curl -sf http://localhost:5000/healthz >/dev/null 2>&1; do
        intentos=$((intentos + 1))
        if [[ ${intentos} -gt 60 ]]; then
            fail "La aplicación no respondió a tiempo."
            info "Revise el registro: ./start.sh --${ENTORNO} --logs"
            compose logs --tail=30 web
            exit 1
        fi
        sleep 2
    done
    ok "La aplicación responde."

    if [[ "${MIGRAR}" == "si" ]]; then
        aplicar_actualizaciones
    else
        warn "Migraciones y siembra omitidas por --no-migrate."
    fi

    comprobar_ollama
    resumen
}

aplicar_actualizaciones() {
    header "Aplicando actualizaciones"

    # El punto de entrada del contenedor ya migra al arrancar; repetirlo aquí
    # cubre el caso de reiniciar sin recrear y no cuesta nada: `db upgrade` no
    # hace nada si no hay revisiones pendientes.
    # La salida se guarda antes de mirarla: «flask … | grep -q» daba por buena
    # una migración fallida -- grep no encontraba «Running upgrade» y se
    # informaba de que la base de datos ya estaba al día.
    local migracion
    if ! migracion="$(compose exec -T web flask db upgrade 2>&1)"; then
        fail "Las migraciones han fallado."
        echo "${migracion}" | tail -20 | sed 's/^/    /' >&2
        exit 1
    fi
    if grep -q 'Running upgrade' <<<"${migracion}"; then
        ok "Migraciones aplicadas."
    else
        ok "La base de datos ya estaba al día."
    fi

    # Idempotente: crea lo que falte, nunca sobrescribe lo que un administrador
    # haya ajustado.
    local salida
    salida="$(compose exec -T web flask seed 2>&1 | grep -E '^(Roles|Ajustes|Reglas|Fuentes|Países)' || true)"
    if [[ -n "${salida}" ]]; then
        echo "${salida}" | sed 's/^/    /'
    fi
    ok "Datos de referencia al día."

    if ! compose exec -T web python -c "
from app import create_app
from app.models.enums import RoleCode
from app.models.user import User
app = create_app()
with app.app_context():
    raise SystemExit(0 if any(u.has_role(RoleCode.ADMINISTRADOR) for u in User.query.all()) else 1)
" >/dev/null 2>&1; then
        warn "No hay ningún administrador."
        info "Créelo con: docker compose -f docker/docker-compose.yml exec web flask create-admin"
    fi
}

comprobar_ollama() {
    local url host
    url="$(grep -E '^OLLAMA_BASE_URL=' "${ENV_FILE}" | cut -d= -f2- || echo '')"
    [[ -z "${url}" ]] && return 0
    host="${url/host.docker.internal/localhost}"

    if curl -sf "${host}/api/tags" >/dev/null 2>&1; then
        ok "Ollama responde en ${host}."
    else
        warn "Ollama no responde en ${host}."
        info "Las funciones de IA fallarán hasta que lo arranque."
        info "Instálelo desde https://ollama.com y ejecute: ollama pull llama3.1:8b"
    fi
}

# Dónde está la aplicación, como la escribiría alguien en el navegador. Decía
# siempre http://localhost:80, también cuando todo va por HTTPS.
direccion_publica() {
    if [[ "${ENTORNO}" == "dev" ]]; then
        local puerto
        puerto="$(grep -E '^DEV_PORT=' "${ENV_FILE}" | cut -d= -f2- || true)"
        printf 'http://localhost:%s' "${puerto:-5000}"
    elif [[ "${FORCE_HTTPS:-}" == "true" ]]; then
        local puerto
        puerto="$(grep -E '^HTTPS_PORT=' "${ENV_FILE}" | cut -d= -f2- || true)"
        puerto="${puerto:-443}"
        [[ "${puerto}" == "443" ]] && printf 'https://%s' "$(hostname -f 2>/dev/null || hostname)" \
            || printf 'https://%s:%s' "$(hostname -f 2>/dev/null || hostname)" "${puerto}"
    else
        local puerto
        puerto="$(grep -E '^HTTP_PORT=' "${ENV_FILE}" | cut -d= -f2- || true)"
        printf 'http://%s:%s' "$(hostname -f 2>/dev/null || hostname)" "${puerto:-80}"
    fi
}

resumen() {
    header "Travel Manager en marcha (${ETIQUETA})"
    echo ""
    echo -e "  ${BOLD}Aplicación:${NC}     $(direccion_publica)"
    if [[ "${ENTORNO}" == "dev" ]]; then
        echo -e "  ${BOLD}PostgreSQL:${NC}     localhost:5432"
        echo -e "  ${BOLD}Redis:${NC}          localhost:6379"
        echo ""
        warn "Antivirus desactivado en desarrollo (ANTIVIRUS_BACKEND=noop)."
    fi
    echo ""
    echo -e "  ${DIM}./start.sh --${ENTORNO} --logs     seguir el registro${NC}"
    echo -e "  ${DIM}./start.sh --${ENTORNO} --stop     detener${NC}"
    echo ""
}

# ---------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------
comprobar_requisitos
echo "${ENTORNO}" > "${STATE_FILE}"

case "${ACCION}" in
    stop)   detener ;;
    status) mostrar_estado ;;
    start)
        arrancar
        if [[ "${SEGUIR_LOGS}" == "si" ]]; then
            header "Registro (Ctrl+C para salir)"
            compose logs -f web worker
        fi
        ;;
esac
