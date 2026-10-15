# shellcheck shell=bash
#
# Proxy de construcción, compartido por setup.sh y start.sh.
#
# En una red que solo sale a internet a través de un proxy, construir las
# imágenes necesita el proxy en DOS sitios distintos, y faltar en cualquiera
# de los dos da un error que no se parece al problema:
#
#   1. El demonio de Docker, que descarga las imágenes base (FROM python…,
#      FROM nginx…) y las de postgres, redis, seaweedfs y clamav. No lee los
#      --build-arg: solo su propia configuración. Sin ella intenta resolver
#      auth.docker.io con el DNS interno y falla con «no such host».
#   2. La construcción en sí -- pip, apt --, que sí usa los --build-arg.
#
# Lo segundo se hace aquí. Lo primero no: configurarlo exige reiniciar el
# demonio, y eso para todos los contenedores del servidor, no solo los de esta
# aplicación. Es una decisión de quien administra la máquina, así que aquí se
# comprueba y, si falta, se dice exactamente qué hacer.
#
# Necesita que quien lo cargue defina ROOT, ENV_FILE y las funciones compose,
# info, warn y fail.

# Lee una variable de un fichero .env sin ejecutarlo.
_leer_de_env() {
    local fichero="$1" clave="$2"
    [[ -f "${fichero}" ]] || return 0
    grep -E "^[[:space:]]*${clave}[[:space:]]*=" "${fichero}" \
        | tail -1 | cut -d= -f2- | tr -d '"'"'"' ' || true
}

# La URL del proxy de construcción, o vacío si esta máquina sale directa.
#
# Se busca en docker/.env, que es donde la plantilla pone BUILD_PROXY y el
# fichero que usa todo lo demás. Antes se leía del .env de la raíz, que
# setup.sh no crea: el valor estaba escrito y no lo leía nadie. Se sigue
# aceptando allí por las instalaciones que lo pusieron en ese sitio.
proxy_de_construccion() {
    # «--proxy URL» en start.sh: para esta vez, por encima de lo configurado.
    if [[ -n "${PROXY_FORZADO:-}" ]]; then
        printf '%s' "${PROXY_FORZADO}"
        return 0
    fi

    local valor
    valor="$(_leer_de_env "${ENV_FILE}" BUILD_PROXY)"
    [[ -n "${valor}" ]] || valor="$(_leer_de_env "${ROOT}/.env" BUILD_PROXY)"
    printf '%s' "${valor:-${HTTPS_PROXY:-${https_proxy:-}}}"
}

# Lo que no debe pasar por el proxy durante la construcción.
sin_proxy_de_construccion() {
    local valor
    valor="$(_leer_de_env "${ENV_FILE}" BUILD_NO_PROXY)"
    printf '%s' "${valor:-${NO_PROXY:-${no_proxy:-localhost,127.0.0.1}}}"
}

# Una URL de proxy apta para mostrarla: sin usuario ni contraseña.
#
# «${proxy%%@*}», que es lo que se usaba, hace lo contrario de lo que parece:
# de «http://usuario:clave@proxy:3128» deja «http://usuario:clave», y la
# contraseña acababa en la pantalla y en el registro de la instalación.
sin_credenciales() {
    printf '%s' "$1" | sed -E 's#(://)[^/@]*@#\1#'
}

# El proxy que tiene configurado el demonio de Docker, o vacío.
#
# Uno y otro por separado: «docker info» devuelve una cadena vacía, con éxito,
# cuando el campo no está, así que encadenarlos con «||» nunca miraba el
# segundo.
proxy_del_demonio() {
    local https http
    https="$(docker info --format '{{.HTTPSProxy}}' 2>/dev/null || true)"
    http="$(docker info --format '{{.HTTPProxy}}' 2>/dev/null || true)"
    printf '%s' "${https:-${http}}"
}

# Falla, explicando cómo arreglarlo, si esta máquina necesita un proxy y el
# demonio de Docker no lo tiene. Mejor aquí, con instrucciones, que a mitad de
# la construcción con un «no such host» que apunta al DNS.
comprobar_proxy_del_demonio() {
    local proxy="$1"
    [[ -n "${proxy}" ]] || return 0

    local del_demonio
    del_demonio="$(proxy_del_demonio)"
    if [[ -n "${del_demonio}" ]]; then
        info "El demonio de Docker sale por $(sin_credenciales "${del_demonio}")."
        return 0
    fi

    fail "Esta máquina necesita un proxy ($(sin_credenciales "${proxy}")), pero el demonio de Docker no lo tiene configurado."
    cat >&2 <<EOF

    Es el demonio, y no la construcción, quien descarga las imágenes base
    (python, nginx, postgres…). No lee el proxy de docker/.env: solo su propia
    configuración. Configúrelo una vez, como root.

    1. Cree el fichero /etc/systemd/system/docker.service.d/http-proxy.conf
       (el directorio puede no existir) con este contenido:

[Service]
Environment="HTTP_PROXY=${proxy}"
Environment="HTTPS_PROXY=${proxy}"
Environment="NO_PROXY=localhost,127.0.0.1"

    2. Recargue y reinicie Docker:

      systemctl daemon-reload
      systemctl restart docker

    Atención: reiniciar Docker detiene todos los contenedores de este servidor,
    no solo los de Travel Manager. Compruébelo después con:

      docker info | grep -i proxy

    y vuelva a ejecutar este script.

EOF
    return 1
}

# docker compose build, con el proxy si hace falta.
#
# Se pasan las cuatro variables, en mayúsculas y en minúsculas, porque Docker
# predefine estos argumentos pero no replica el caso: pasando solo
# «HTTPS_PROXY», pip y apt -- que leen la forma en minúsculas -- seguirían
# saliendo directos, y parecería funcionar sin hacer nada.
#
# Solo durante la construcción. Ni se exporta al shell ni llega a los
# contenedores: la aplicación tiene su propio proxy de salida en Ajustes, y
# meterlo también por el entorno daría dos fuentes para la misma decisión.
construir_imagenes() {
    local proxy sin_proxy
    proxy="$(proxy_de_construccion)"

    comprobar_proxy_del_demonio "${proxy}" || return 1

    if [[ -z "${proxy}" ]]; then
        compose build || { _pista_si_falla_la_construccion; return 1; }
        return
    fi

    sin_proxy="$(sin_proxy_de_construccion)"
    info "Construyendo a través del proxy $(sin_credenciales "${proxy}")"
    HTTP_PROXY="${proxy}" HTTPS_PROXY="${proxy}" \
    http_proxy="${proxy}" https_proxy="${proxy}" \
    compose build \
        --build-arg HTTP_PROXY="${proxy}" \
        --build-arg HTTPS_PROXY="${proxy}" \
        --build-arg http_proxy="${proxy}" \
        --build-arg https_proxy="${proxy}" \
        --build-arg NO_PROXY="${sin_proxy}" \
        --build-arg no_proxy="${sin_proxy}" \
        || { _pista_si_falla_la_construccion; return 1; }
}

# Qué probar cuando la construcción falla por algo del servidor y no del
# proyecto. Visto en producción: «mount source: overlay ... no such device» al
# copiar el código, con el Dockerfile intacto. No se puede distinguir desde
# aquí cuál de las dos causas es, así que se dicen las dos, en el orden en que
# conviene probarlas: la primera no toca nada más que la caché.
_pista_si_falla_la_construccion() {
    cat >&2 <<'PISTA'

    Si el error de arriba menciona «overlay», «no such device» o «failed to
    solve: mount», no es del proyecto: es Docker en este servidor.

    1. La caché de construcción puede haber quedado dañada, por ejemplo al
       reiniciar Docker a mitad de una construcción. Bórrela y repita:

         docker builder prune -af

    2. Si sigue igual, el núcleo puede no tener disponible el módulo
       «overlay», típicamente tras actualizar el núcleo sin reiniciar.
       Compruébelo:

         uname -r; ls /lib/modules; modprobe overlay && echo "overlay: ok"

       Si «uname -r» no aparece en /lib/modules o modprobe falla, reinicie
       el servidor.

PISTA
}
