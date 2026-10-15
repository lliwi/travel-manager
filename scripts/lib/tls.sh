# shellcheck shell=bash
#
# HTTP o HTTPS, compartido por setup.sh y start.sh.
#
# Las dos piezas que deciden tienen que decidir lo mismo: Nginx (redirigir o
# no) y la aplicación (FORCE_HTTPS, y con él si la cookie de sesión es solo
# HTTPS). Antes setup.sh no decidía nada -- la aplicación redirigía por su
# cuenta -- y start.sh, con el certificado autofirmado, servía HTTP mientras la
# cookie seguía marcada «Secure»: el navegador la descartaba y por http:// no
# se podía iniciar sesión, con un «error inesperado» por toda explicación.
#
# La regla: con certificado, sea el autofirmado o uno de verdad, todo va por
# HTTPS. Con el autofirmado el navegador avisa la primera vez, pero la sesión
# funciona y sigue siendo solo HTTPS. Solo sin certificado se sirve HTTP, y
# entonces la cookie no puede ser «Secure» o nadie podría entrar.
#
# Necesita que quien lo cargue defina ROOT y las funciones ok, info y warn.

configurar_tls() {
    local dir="${ROOT}/docker/nginx/certs"
    local destino="${ROOT}/docker/nginx/tls_redirect.conf"
    local cert="${dir}/server.crt"
    local cabecera="# Generado por setup.sh / start.sh -- no editar a mano."

    if [[ ! -s "${cert}" || ! -s "${dir}/server.key" ]]; then
        printf '%s\n# Sin certificado: se sirve por HTTP.\n' "${cabecera}" > "${destino}"
        export FORCE_HTTPS=false
        warn "No hay certificado en docker/nginx/certs: se sirve por HTTP, y la sesión viaja sin cifrar."
        return 0
    fi

    printf '%s\nreturn 301 https://$host$request_uri;\n' "${cabecera}" > "${destino}"
    export FORCE_HTTPS=true

    if ! command -v openssl >/dev/null 2>&1; then
        ok "Hay certificado: todo se sirve por HTTPS."
        return 0
    fi

    local emisor sujeto
    emisor="$(openssl x509 -noout -issuer -in "${cert}" 2>/dev/null | sed 's/^issuer=//')"
    sujeto="$(openssl x509 -noout -subject -in "${cert}" 2>/dev/null | sed 's/^subject=//')"

    if [[ -n "${emisor}" && "${emisor}" == "${sujeto}" ]]; then
        ok "Certificado autofirmado: todo se sirve por HTTPS."
        info "El navegador avisará del certificado. Sustitúyalo por uno de su CA en docker/nginx/certs."
    else
        ok "Certificado emitido por «${emisor#*CN=}»: todo se sirve por HTTPS."
    fi

    # Un certificado caducado redirige a un HTTPS que nadie puede usar, y el
    # aviso llega tarde si solo se ve en el navegador.
    if ! openssl x509 -checkend 0 -noout -in "${cert}" >/dev/null 2>&1; then
        warn "El certificado está CADUCADO. Renuévelo: la aplicación redirige a HTTPS."
    elif ! openssl x509 -checkend 2592000 -noout -in "${cert}" >/dev/null 2>&1; then
        warn "El certificado caduca en menos de 30 días."
    fi
}
