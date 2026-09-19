#!/usr/bin/env bash
# Travel Manager — first-time installation.
# Generates secrets, builds the images, starts the stack and creates the
# first administrator. Safe to re-run: existing secrets are preserved.
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

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
set_if_placeholder SECRET_KEY "$(secret)"
set_if_placeholder SECRETS_ENCRYPTION_KEY "$(fernet)"
set_if_placeholder POSTGRES_PASSWORD "$(password)"
set_if_placeholder REDIS_PASSWORD "$(password)"
set_if_placeholder S3_SECRET_KEY "$(password)"
warn "docker/.env contiene secretos y está excluido del control de versiones."

# ---------------------------------------------------------------------
header "Paso 4: comprobando Ollama en el host"
# ---------------------------------------------------------------------
OLLAMA_URL="$(grep -E '^OLLAMA_BASE_URL=' "${ENV_FILE}" | cut -d= -f2- || echo '')"
HOST_URL="${OLLAMA_URL/host.docker.internal/localhost}"
if curl -sf "${HOST_URL}/api/tags" >/dev/null 2>&1; then
    ok "Ollama responde en ${HOST_URL}."
    MODEL="$(grep -E '^OLLAMA_DEFAULT_MODEL=' "${ENV_FILE}" | cut -d= -f2- || echo '')"
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
${COMPOSE} build
ok "Imágenes construidas."

# ---------------------------------------------------------------------
header "Paso 7: arrancando los servicios"
# ---------------------------------------------------------------------
${COMPOSE} up -d
ok "Servicios arrancados."
info "ClamAV descarga su base de firmas en el primer arranque; puede tardar"
info "varios minutos hasta que acepte análisis."

header "Paso 8: esperando a que la aplicación responda"
for _ in $(seq 1 60); do
    if ${COMPOSE} exec -T web python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:5000/healthz', timeout=3)" \
        >/dev/null 2>&1; then
        ok "La aplicación responde."
        break
    fi
    sleep 3
done

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
echo -e "  ${BOLD}Aplicación:${NC}       http://localhost:${HTTP_PORT}"
echo -e "  ${BOLD}Consola MinIO:${NC}    http://localhost:9001"
echo ""
echo -e "  ${BOLD}Comandos útiles:${NC}"
echo "    ./start.sh                      arrancar de nuevo"
echo "    docker compose -f docker/docker-compose.yml logs -f web"
echo "    docker compose -f docker/docker-compose.yml exec web flask ai-health"
echo "    docker compose -f docker/docker-compose.yml exec web flask verify-audit"
echo ""
