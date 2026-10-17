#!/usr/bin/env bash
# Travel Manager — first-time installation.
# Generates secrets, builds the images, starts the stack and creates the
# first administrator. Safe to re-run: existing secrets are preserved, and it
# refuses to generate new ones over the data of a previous installation.
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

# Opciones. Sin ninguna, aplica lo que diga docker/.env: si alguien cambió ahí
# una credencial, el sistema se reconfigura con ella. Con --rotar genera
# credenciales nuevas para todo, que es lo que se hace ante un compromiso.
ROTAR=""
COPIA_ENV=""
for arg in "$@"; do
    case "${arg}" in
        --rotar) ROTAR="si" ;;
        -h|--help)
            cat <<'USO'
Uso: ./setup.sh [--rotar]

  (sin opciones)  Instalar, o aplicar lo que diga docker/.env. Si se ha
                  cambiado ahí una credencial, el sistema se reconfigura.
  --rotar         Generar credenciales nuevas para todo y aplicarlas: clave
                  de sesión, contraseñas de PostgreSQL, Redis y S3, el token
                  de métricas si lo hay, y la clave de cifrado, re-cifrando
                  con ella lo guardado. Las sesiones abiertas se cierran.
USO
            exit 0 ;;
        *) echo "Opción desconocida: ${arg}" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ROOT}/docker/.env"
COMPOSE="docker compose -f ${ROOT}/docker/docker-compose.yml --env-file ${ENV_FILE}"

header()  { echo -e "\n${BOLD}${BLUE}==> $1${NC}"; }
ok()      { echo -e "  ${GREEN}✓${NC} $1"; }
warn()    { echo -e "  ${YELLOW}!${NC} $1"; }
fail()    { echo -e "  ${RED}✗${NC} $1"; }
info()    { echo -e "    $1"; }

secret()  { python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32; }
password() { python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(28)))" 2>/dev/null || openssl rand -base64 24 | tr -d '/+='; }
fernet()  {
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null \
        || python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
}

# Write a variable into the env file, replacing it or appending it.
escribir_env() {
    python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys, re, pathlib
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
text = p.read_text()
pattern = re.compile(rf'^{re.escape(key)}=.*$', re.M)
if pattern.search(text):
    text = pattern.sub(lambda _m: f'{key}={value}', text, count=1)
else:
    text = text.rstrip('\n') + f'\n{key}={value}\n'
p.write_text(text)
PY
}

leer_env() {
    grep -E "^$1=" "${ENV_FILE}" | head -1 | cut -d= -f2- || true
}

# Set a variable in the env file only if it still holds a placeholder.
set_if_placeholder() {
    local key="$1" value="$2"
    local current
    current="$(grep -E "^${key}=" "${ENV_FILE}" | head -1 | cut -d= -f2- || true)"
    if [[ -z "${current}" || "${current}" == cambie-* ]]; then
        python3 - "$ENV_FILE" "$key" "$value" <<'PY'
import sys, re, pathlib
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
text = p.read_text()
pattern = re.compile(rf'^{re.escape(key)}=.*$', re.M)
if pattern.search(text):
    text = pattern.sub(f'{key}={value}', text, count=1)
else:
    text = text.rstrip('\n') + f'\n{key}={value}\n'
p.write_text(text)
PY
        ok "${key} generada."
    else
        info "${key} ya estaba configurada; se conserva."
    fi
}

# ---------------------------------------------------------------------
header "Paso 1: comprobando requisitos"
# ---------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || { fail "Docker no está instalado."; exit 1; }
ok "Docker encontrado: $(docker --version | cut -d, -f1)"

docker compose version >/dev/null 2>&1 || { fail "Se requiere Docker Compose v2."; exit 1; }
ok "Docker Compose encontrado."

docker info >/dev/null 2>&1 || { fail "El demonio de Docker no responde. ¿Está arrancado?"; exit 1; }
ok "El demonio de Docker responde."

command -v python3 >/dev/null 2>&1 || warn "python3 no está disponible; se usará openssl para los secretos."

# ---------------------------------------------------------------------
header "Paso 2: preparando la configuración"
# ---------------------------------------------------------------------
mkdir -p "${ROOT}/docker"

# Repetir la instalación es seguro mientras docker/.env siga ahí: los secretos
# se conservan. El caso que no lo es: datos de una instalación anterior y un
# docker/.env ausente o con un secreto vacío -- típicamente, el código copiado
# de nuevo sin traer el .env --. Seguir generaría secretos nuevos encima de
# esos datos: con otra SECRETS_ENCRYPTION_KEY las claves de API, la del
# directorio, los secretos MFA y los números de documento quedan ilegibles
# para siempre, y con otra POSTGRES_PASSWORD la base de datos la rechaza.
# Se detiene antes de escribir nada.
VOLUMEN_DATOS="$(${COMPOSE} config --format json 2>/dev/null \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["name"])' 2>/dev/null \
    || echo travel-manager)_postgres_data"

falta_un_secreto() {
    [[ ! -f "${ENV_FILE}" ]] && return 0
    local clave valor
    for clave in SECRETS_ENCRYPTION_KEY POSTGRES_PASSWORD; do
        valor="$(grep -E "^${clave}=" "${ENV_FILE}" | head -1 | cut -d= -f2- || true)"
        [[ -z "${valor}" || "${valor}" == cambie-* ]] && return 0
    done
    return 1
}

if docker volume inspect "${VOLUMEN_DATOS}" >/dev/null 2>&1 && falta_un_secreto; then
    fail "Hay datos de una instalación anterior (${VOLUMEN_DATOS}), pero falta docker/.env o uno de sus secretos."
    cat >&2 <<'AVISO'

    Seguir generaría secretos nuevos encima de esos datos, y no tiene vuelta
    atrás: las claves de API, la contraseña del directorio, los segundos
    factores y los números de documento quedarían ilegibles, y PostgreSQL
    rechazaría la contraseña nueva. No se ha modificado nada.

    Recupere el docker/.env de esa instalación -- de la carpeta anterior del
    proyecto, de una copia de seguridad, o de docker/.env.<fecha>.bak, que este
    script deja antes de tocarlo -- y vuelva a ejecutar ./setup.sh.

    Si de verdad quiere empezar de cero y BORRAR todos los datos:

      docker compose -f docker/docker-compose.yml down -v

AVISO
    exit 1
fi

if [[ -f "${ENV_FILE}" ]]; then
    # Antes de tocarlo: si algo saliera mal, el anterior sigue ahí.
    COPIA_ENV="${ENV_FILE}.$(date +%Y%m%d-%H%M%S).bak"
    cp -p "${ENV_FILE}" "${COPIA_ENV}"
    chmod 600 "${COPIA_ENV}"
    info "Copia del docker/.env actual en ${COPIA_ENV#"${ROOT}"/}."
fi

if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${ROOT}/.env.example" "${ENV_FILE}"
    ok "docker/.env creado a partir de la plantilla."
else
    info "docker/.env ya existe; solo se rellenarán los valores pendientes."
    # Back-fill any variable added to the template since this file was created.
    python3 - "$ROOT/.env.example" "$ENV_FILE" <<'PY'
import pathlib, re, sys
plantilla, destino = (pathlib.Path(a) for a in sys.argv[1:3])
existentes = {
    m.group(1)
    for m in re.finditer(r'^([A-Z_0-9]+)=', destino.read_text(), re.M)
}
faltan = [
    line for line in plantilla.read_text().splitlines()
    if (m := re.match(r'^([A-Z_0-9]+)=', line)) and m.group(1) not in existentes
]
if faltan:
    with destino.open('a') as f:
        f.write('\n# --- Añadidas por setup.sh ---\n')
        f.write('\n'.join(faltan) + '\n')
    print(f'    Se han añadido {len(faltan)} variables nuevas.')
PY
fi
chmod 600 "${ENV_FILE}"

# ---------------------------------------------------------------------
header "Paso 3: generando secretos"
# ---------------------------------------------------------------------
if [[ -n "${ROTAR}" ]]; then
    # La clave de cifrado no se sustituye sin más: lo guardado está cifrado con
    # la actual, que pasa a «anterior» para poder re-cifrarlo en el paso 8. Si
    # ya había una anterior es que una rotación quedó a medias, y pisarla
    # perdería lo que solo ella abre.
    if [[ -n "$(leer_env SECRETS_ENCRYPTION_KEY_PREVIOUS)" ]]; then
        fail "SECRETS_ENCRYPTION_KEY_PREVIOUS ya tiene valor: una rotación anterior no terminó."
        info "Ejecute ./setup.sh sin --rotar para completarla, y luego repita."
        exit 1
    fi
    escribir_env SECRETS_ENCRYPTION_KEY_PREVIOUS "$(leer_env SECRETS_ENCRYPTION_KEY)"
    escribir_env SECRETS_ENCRYPTION_KEY "$(fernet)"
    escribir_env SECRET_KEY "$(secret)"
    escribir_env POSTGRES_PASSWORD "$(password)"
    escribir_env REDIS_PASSWORD "$(password)"
    escribir_env S3_SECRET_KEY "$(password)"
    [[ -n "$(leer_env METRICS_TOKEN)" ]] && escribir_env METRICS_TOKEN "$(secret)"
    ok "Credenciales nuevas generadas en docker/.env."
    [[ -n "${COPIA_ENV}" ]] && info "Las anteriores quedan en ${COPIA_ENV#"${ROOT}"/}, por si hubiera que volver atrás."
fi
set_if_placeholder SECRET_KEY "$(secret)"
set_if_placeholder SECRETS_ENCRYPTION_KEY "$(fernet)"
set_if_placeholder POSTGRES_PASSWORD "$(password)"
set_if_placeholder REDIS_PASSWORD "$(password)"
set_if_placeholder S3_SECRET_KEY "$(password)"
warn "docker/.env contiene secretos y está excluido del control de versiones."

# ---------------------------------------------------------------------
header "Paso 4: comprobando Ollama en el host"
# ---------------------------------------------------------------------
# Los nombres son «OLLAMA_BOOTSTRAP_*» desde que la configuración de IA pasó a
# la base de datos; este paso seguía leyendo los de antes y no encontraba nada,
# así que avisaba de que «Ollama no responde en .» —sin URL— en una máquina
# donde podía estar respondiendo perfectamente. Y vienen comentados en la
# plantilla, porque el valor útil es el que trae la propia configuración.
OLLAMA_URL="$(grep -E '^[[:space:]]*OLLAMA_BOOTSTRAP_URL=' "${ENV_FILE}" | tail -1 | cut -d= -f2- || true)"
OLLAMA_URL="${OLLAMA_URL:-http://host.docker.internal:11434}"
HOST_URL="${OLLAMA_URL/host.docker.internal/localhost}"
if curl -sf "${HOST_URL}/api/tags" >/dev/null 2>&1; then
    ok "Ollama responde en ${HOST_URL}."
    MODEL="$(grep -E '^[[:space:]]*OLLAMA_BOOTSTRAP_MODEL=' "${ENV_FILE}" | tail -1 | cut -d= -f2- || true)"
    MODEL="${MODEL:-llama3.1:8b}"
    if curl -sf "${HOST_URL}/api/tags" | grep -q "${MODEL%%:*}"; then
        ok "El modelo «${MODEL}» está descargado."
    else
        warn "El modelo «${MODEL}» no está descargado. Ejecute: ollama pull ${MODEL}"
    fi
else
    warn "Ollama no responde en ${HOST_URL}."
    info "La aplicación arrancará igualmente, pero las funciones de IA fallarán."
    info "Instale Ollama desde https://ollama.com y ejecute: ollama pull llama3.1:8b"
fi

# ---------------------------------------------------------------------
header "Paso 5: generando certificado TLS de desarrollo"
# ---------------------------------------------------------------------
CERT_DIR="${ROOT}/docker/nginx/certs"
mkdir -p "${CERT_DIR}"
if [[ -f "${CERT_DIR}/server.crt" ]]; then
    info "Ya existe un certificado; se conserva."
elif command -v openssl >/dev/null 2>&1; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
        -keyout "${CERT_DIR}/server.key" -out "${CERT_DIR}/server.crt" \
        -subj "/C=ES/O=Travel Manager/CN=localhost" >/dev/null 2>&1
    chmod 600 "${CERT_DIR}/server.key"
    ok "Certificado autofirmado generado (solo para desarrollo)."
    warn "En producción, sustitúyalo por un certificado emitido por una CA."
else
    warn "openssl no disponible; no se ha generado certificado."
fi

# ---------------------------------------------------------------------
header "Paso 6: construyendo las imágenes"
# ---------------------------------------------------------------------
info "Esto puede tardar varios minutos la primera vez."
# Con el proxy de BUILD_PROXY si esta máquina no sale directa, y comprobando
# antes que el demonio de Docker también lo tiene: sin eso la construcción
# falla en la primera línea con un «no such host» que apunta al DNS.
compose() { ${COMPOSE} "$@"; }
# shellcheck source=scripts/lib/proxy.sh
source "${ROOT}/scripts/lib/proxy.sh"
if ! construir_imagenes; then
    fail "No se han podido construir las imágenes."
    exit 1
fi
ok "Imágenes construidas."

# ---------------------------------------------------------------------
header "Paso 7: arrancando los servicios"
# ---------------------------------------------------------------------
# La misma decisión que start.sh sobre HTTP y HTTPS. Sin esto, tras la
# instalación la aplicación redirigía por su cuenta y Nginx no, y al primer
# «start.sh» la decisión cambiaba.
# shellcheck source=scripts/lib/tls.sh
source "${ROOT}/scripts/lib/tls.sh"
configurar_tls

# PostgreSQL guarda su contraseña dentro del volumen y solo toma la de
# docker/.env al crearlo. Se le da la de ahora antes de arrancar la
# aplicación: es lo que hace que cambiarla en el .env -- o rotarla -- funcione,
# y lo que arregla una que se quedó desfasada. Desde dentro del contenedor no
# pide contraseña, así que no hace falta saber la antigua. Va por la entrada
# estándar, no en la línea de órdenes: ahí la vería cualquiera en la lista de
# procesos.
sincronizar_postgres() {
    local usuario clave
    usuario="$(leer_env POSTGRES_USER)"; usuario="${usuario:-travel}"
    clave="$(leer_env POSTGRES_PASSWORD)"
    ${COMPOSE} up -d postgres >/dev/null 2>&1
    # Reintentando, y no fiándose de pg_isready: en el primer arranque
    # PostgreSQL crea la base con un servidor provisional, lo para y arranca
    # el definitivo, y pg_isready ya dice «listo» durante ese cambio. Visto en
    # una instalación limpia: el ALTER caía justo en el reinicio.
    local intento
    for intento in $(seq 1 30); do
        if printf 'ALTER USER "%s" WITH PASSWORD '"'"'%s'"'"';\n' \
                "${usuario//\"/\"\"}" "${clave//\'/\'\'}" \
            | ${COMPOSE} exec -T postgres sh -c \
                'psql -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
                >/dev/null 2>&1; then
            ok "Contraseña de PostgreSQL aplicada."
            return 0
        fi
        sleep 2
    done
    fail "No se pudo aplicar la contraseña de PostgreSQL tras ${intento} intentos."
    info "Registro de PostgreSQL:"
    ${COMPOSE} logs --tail=20 postgres >&2 || true
    exit 1
}
sincronizar_postgres

# --remove-orphans: al repetirlo tras una actualización retira los servicios
# que ya no existen -- el de MinIO, por ejemplo --, que si no seguirían en
# marcha sin que nadie los use.
${COMPOSE} up -d --remove-orphans
ok "Servicios arrancados."
info "ClamAV descarga su base de firmas en el primer arranque; puede tardar"
info "varios minutos hasta que acepte análisis."

header "Paso 8: esperando a que la aplicación responda"
RESPONDE=""
for _ in $(seq 1 60); do
    if ${COMPOSE} exec -T web python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)" \
        >/dev/null 2>&1; then
        RESPONDE="si"
        ok "La aplicación responde."
        break
    fi
    sleep 3
done
# Antes seguía igualmente, y el paso siguiente -- que pregunta a la aplicación
# si hay administrador -- tomaba el silencio por un «no» y pedía crear uno.
if [[ -z "${RESPONDE}" ]]; then
    fail "La aplicación no ha respondido en tres minutos."
    info "Últimas líneas de su registro:"
    ${COMPOSE} logs --tail=30 web >&2 || true
    exit 1
fi

# ---------------------------------------------------------------------
header "Paso 8b: comprobando el cifrado de lo guardado"
# ---------------------------------------------------------------------
# Si la clave de cifrado ha cambiado, lo guardado se re-cifra con la nueva
# usando la anterior. Sin la anterior se para sin escribir nada: re-cifrar lo
# demás escondería que hay secretos que ya no se pueden leer.
# La clave va por la entrada estándar y no con «exec -e»: en la línea de
# órdenes la vería cualquiera en la lista de procesos del servidor.
rotar_cifrado_con() {
    printf '%s\n' "$1" | ${COMPOSE} exec -T web sh -c \
        'read -r ANTERIOR; SECRETS_ENCRYPTION_KEY_PREVIOUS="${ANTERIOR:-$SECRETS_ENCRYPTION_KEY_PREVIOUS}" flask rotar-cifrado '"$2" 2>&1
}

if ! SALIDA_CIFRADO="$(rotar_cifrado_con '' '')"; then
    # Se ha cambiado la clave sin decir cuál era la anterior. Este script deja
    # una copia de docker/.env cada vez que lo toca, así que se buscan en ellas,
    # de la más reciente a la más antigua, y se usa la primera que abra TODO --
    # no basta con una parte: re-cifrar lo demás escondería lo que se pierde.
    ANTERIOR_ENCONTRADA=""
    while IFS= read -r copia; do
        candidata="$(grep -E '^SECRETS_ENCRYPTION_KEY=' "${copia}" | head -1 | cut -d= -f2- || true)"
        [[ -z "${candidata}" || "${candidata}" == "$(leer_env SECRETS_ENCRYPTION_KEY)" ]] && continue
        if rotar_cifrado_con "${candidata}" '--comprobar' >/dev/null; then
            ANTERIOR_ENCONTRADA="${candidata}"
            info "La clave anterior estaba en ${copia#"${ROOT}"/}; se usa para re-cifrar."
            break
        fi
    done < <(ls -t "${ROOT}"/docker/.env.*.bak 2>/dev/null)

    if [[ -z "${ANTERIOR_ENCONTRADA}" ]] \
        || ! SALIDA_CIFRADO="$(rotar_cifrado_con "${ANTERIOR_ENCONTRADA}" '')"; then
        grep -v '^{' <<<"${SALIDA_CIFRADO}" | sed 's/^/    /' >&2
        fail "Hay secretos que no se abren con SECRETS_ENCRYPTION_KEY."
        info "Ninguna copia docker/.env.*.bak tiene la clave con que se cifraron."
        info "Póngala en SECRETS_ENCRYPTION_KEY_PREVIOUS y vuelva a ejecutar ./setup.sh."
        info "No se ha modificado ningún secreto guardado."
        exit 1
    fi
fi
ok "$(grep -v '^{' <<<"${SALIDA_CIFRADO}" | tail -1)"

# Todo está ya con la clave actual: la anterior no hace falta, y después de un
# compromiso conservarla sería no haber rotado nada.
if [[ -n "$(leer_env SECRETS_ENCRYPTION_KEY_PREVIOUS)" ]]; then
    escribir_env SECRETS_ENCRYPTION_KEY_PREVIOUS ""
    ${COMPOSE} up -d >/dev/null 2>&1
    for _ in $(seq 1 60); do
        ${COMPOSE} exec -T web python -c \
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)" \
            >/dev/null 2>&1 && break
        sleep 3
    done
    ok "Clave de cifrado anterior retirada: todo está cifrado con la nueva."
fi

# ---------------------------------------------------------------------
header "Paso 9: creando el usuario administrador"
# ---------------------------------------------------------------------
if ${COMPOSE} exec -T web python -c "
from app import create_app
from app.models.enums import RoleCode
from app.models.user import User
app = create_app()
with app.app_context():
    existe = any(u.has_role(RoleCode.ADMINISTRADOR) for u in User.query.all())
    raise SystemExit(0 if existe else 1)
" >/dev/null 2>&1; then
    info "Ya existe un administrador; se omite este paso."
else
    echo ""
    info "Introduzca los datos del primer administrador:"
    ${COMPOSE} exec web flask create-admin
fi

# ---------------------------------------------------------------------
header "Instalación completada"
# ---------------------------------------------------------------------
HTTP_PORT="$(grep -E '^HTTP_PORT=' "${ENV_FILE}" | cut -d= -f2- || echo 80)"
echo ""
if [[ "${FORCE_HTTPS:-}" == "true" ]]; then
    echo -e "  ${BOLD}Aplicación:${NC}       https://$(hostname -f 2>/dev/null || hostname)"
else
    echo -e "  ${BOLD}Aplicación:${NC}       http://$(hostname -f 2>/dev/null || hostname):${HTTP_PORT}"
fi
echo ""
echo -e "  ${BOLD}Comandos útiles:${NC}"
echo "    ./start.sh                      arrancar de nuevo"
echo "    docker compose -f docker/docker-compose.yml logs -f web"
echo "    docker compose -f docker/docker-compose.yml exec web flask ai-health"
echo "    docker compose -f docker/docker-compose.yml exec web flask verify-audit"
echo ""
