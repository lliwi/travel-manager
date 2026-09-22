"""Outbound HTTP, through a corporate proxy when there is one.

Everything this application sends outside goes through :func:`cliente`: the
flight and lodging connector, the public advisory sources and the external AI
providers. In an organisation where the servers do not reach the internet
directly, that is the difference between a working deployment and one where
three unrelated features are mysteriously slow and then time out.

**What must not go through it.** Local inference is the case that matters. An
Ollama running beside the application is reached at ``host.docker.internal``,
and sending that through a corporate proxy either fails or -- worse -- succeeds
slowly while the proxy logs every prompt. The same code path serves a local
vLLM and a hosted OpenAI, so the bypass cannot be decided by which provider it
is; it is decided by the address, which is what :data:`EXCEPCIONES_POR_DEFECTO`
is for.

**Where the proxy is configured.** In Ajustes, with the environment as the
fallback. One precedence rule, in that order: a proxy set in the panel wins,
and when none is set the standard ``HTTPS_PROXY``/``NO_PROXY`` variables are
honoured, because that is what whoever builds the image will already have done.
Two sources with no stated order is how a setting ends up being ignored by
something nobody thought to check.

**TLS interception.** A corporate proxy often re-signs certificates with its
own authority. That is not configured here: ``trust_env`` stays on, so
``SSL_CERT_FILE`` pointing at the corporate CA bundle works as it does for
every other Python program.

**The request's budget.** A web request has a hard ceiling -- Gunicorn kills
the worker at ``WEB_REQUEST_TIMEOUT`` -- and one screen can make several
outbound calls. Each call knowing its own timeout is not enough: the planning
assistant asked the flight connector for 25 s, the lodging connector for
another 25 and the model for 120, which is 170 against a ceiling of 120.
Gunicorn won, and winning means SIGABRT: the route's own error handling never
ran, the user got a bare 500, and the ``ai_runs`` row that says what we sent
outside died with the transaction. So the clamp lives here rather than in each
caller -- a timeout longer than what is left of the request cannot be asked
for, and whoever adds the next outbound call inherits it without knowing.
"""

import ipaddress
import logging
import time

import httpx
from flask import current_app, g, has_request_context

from app.utils.errors import TransientError

logger = logging.getLogger(__name__)

#: Reserved for what still has to happen after the last outbound call returns:
#: commit the transaction, render the template, write the response.
MARGEN_DE_RESPUESTA = 5.0

#: Never proxied unless somebody removes them. These are the addresses of
#: things running inside the perimeter -- the other containers, the host, and
#: local inference -- and routing them through an outside proxy turns a local
#: call into a round trip through a machine that has no reason to see it.
EXCEPCIONES_POR_DEFECTO = (
    # IPv6 in brackets: httpx parses the pattern as a URL, and «all://::1»
    # makes it read «:1» as a port and refuse to build the client at all --
    # so one malformed exception would break every outbound call the moment
    # somebody configured a proxy.
    'localhost', '127.0.0.1', '[::1]',
    'host.docker.internal',
    'postgres', 'redis', 'minio', 'openldap', 'mailpit', 'web', 'worker',
)


def _ajuste(clave, defecto=''):
    """Read a setting without requiring the database to be reachable.

    This runs on the path that fetches things, including during start-up
    checks, so a database that is not up yet must degrade to «no proxy
    configured here» rather than raise inside somebody's HTTP call.
    """
    try:
        from app.services import settings_service

        return settings_service.get(clave, defecto)
    except Exception:
        return defecto


def proxy_configurado():
    """The proxy URL from Ajustes, or None to let the environment decide.

    Credentials are kept in their own settings rather than written into the
    URL, and assembled here. A password inside «http://user:pass@proxy:3128»
    is a password stored in a plain string column and rendered on the
    administration screen, which is how it ends up in a browser's history and
    in somebody's screenshot.
    """
    url = (_ajuste('PROXY_SALIDA', '') or '').strip()
    if not url:
        return None

    usuario = (_ajuste('PROXY_USUARIO', '') or '').strip()
    if not usuario:
        return url

    from urllib.parse import quote, urlsplit, urlunsplit

    contrasena = _ajuste('PROXY_CONTRASENA', '') or ''
    partes = urlsplit(url)
    autoridad = f'{quote(usuario, safe="")}'
    if contrasena:
        autoridad += f':{quote(contrasena, safe="")}'
    autoridad += f'@{partes.netloc}'

    return urlunsplit((partes.scheme, autoridad, partes.path, '', ''))


def excepciones():
    """Hosts that must be reached directly, whatever the proxy says."""
    crudo = (_ajuste('PROXY_EXCEPCIONES', '') or '').strip()
    extra = [p.strip() for p in crudo.replace(';', ',').split(',') if p.strip()]
    return list(EXCEPCIONES_POR_DEFECTO) + extra


def _reglas():
    """What must be reached directly, split into the three kinds of rule.

    Returns ``(nombres, redes)``: host names and domain suffixes on one side,
    IP networks on the other. They are matched differently and conflating them
    is how a CIDR ends up compared as a string and never matching anything.
    """
    nombres, redes = [], []
    for entrada in excepciones():
        texto = str(entrada).strip().strip('[]')
        if not texto:
            continue
        try:
            redes.append(ipaddress.ip_network(texto, strict=False))
        except ValueError:
            nombres.append(texto.lower().lstrip('*'))
    return nombres, redes


def _es_directo(host):
    """Whether this host must be reached without the proxy.

    Matching follows what every other NO_PROXY implementation does, and
    deliberately does **not** resolve names: deciding how to route by asking
    DNS first would add a lookup to every request and make the route depend on
    what the resolver happened to answer at that moment. A machine known by
    name is listed by name; a range covers the addresses it contains.
    """
    if not host:
        return False

    limpio = str(host).strip().strip('[]').lower()

    direccion = None
    try:
        direccion = ipaddress.ip_address(limpio)
    except ValueError:
        pass

    if direccion is not None and _directo_si_es_privada():
        # The common meaning of «the local network», without anybody having to
        # write out the ranges: anything not routable from the internet.
        if (direccion.is_private or direccion.is_loopback
                or direccion.is_link_local):
            return True

    nombres, redes = _reglas()

    if direccion is not None:
        return any(direccion in red for red in redes)

    for nombre in nombres:
        if limpio == nombre or limpio.endswith('.' + nombre.lstrip('.')):
            return True
    return False


def _directo_si_es_privada():
    valor = _ajuste('PROXY_DIRECTO_PRIVADAS', True)
    if isinstance(valor, bool):
        return valor
    return str(valor).strip().lower() not in ('false', '0', 'no', '')


class _Enrutador(httpx.BaseTransport):
    """Sends each request through the proxy, or straight out, per destination.

    A transport rather than httpx's per-host mounts because a mount pattern is
    matched as a URL: a range like ``192.168.0.0/16`` is accepted there and
    matches nothing, so the configuration looks applied and the traffic goes to
    the proxy anyway. Deciding here means one rule can be a name, a domain or a
    range, and all three are checked the same way.
    """

    def __init__(self, directo, por_proxy):
        self._directo = directo
        self._por_proxy = por_proxy

    def handle_request(self, request):
        elegido = self._directo if _es_directo(request.url.host) else self._por_proxy
        return elegido.handle_request(request)

    def close(self):
        self._directo.close()
        self._por_proxy.close()


def _bundle_de_certificados():
    """The CA bundle to trust, honouring ``SSL_CERT_FILE`` ourselves.

    A proxy that inspects TLS re-signs certificates with its own authority, and
    pointing that variable at the corporate bundle is how every other Python
    program is told about it. We read it here because taking over the routing
    means turning ``trust_env`` off, and that would otherwise have taken the
    certificates with it.
    """
    import os

    ruta = os.environ.get('SSL_CERT_FILE')
    if ruta and os.path.exists(ruta):
        return ruta
    return True


def marcar_inicio_de_peticion():
    """Start this request's clock. Called once, from the request hooks."""
    g._inicio_de_peticion = time.monotonic()


def presupuesto_restante():
    """Seconds this request may still spend waiting, or None if unbounded.

    None outside a request context on purpose: the Celery worker answers to its
    own task time limits, and a document that takes four minutes to extract is
    doing its job rather than holding a browser open.
    """
    if not has_request_context():
        return None

    inicio = getattr(g, '_inicio_de_peticion', None)
    if inicio is None:
        return None

    techo = current_app.config.get('WEB_REQUEST_TIMEOUT') or 0
    if techo <= 0:
        return None

    return techo - (time.monotonic() - inicio) - MARGEN_DE_RESPUESTA


def _dentro_del_presupuesto(timeout):
    """Trim a timeout to what is left of the request.

    Asking for longer than the request has left is asking to be killed by the
    server instead of failing in the application, and those two are not the
    same thing: one leaves a flash message and an audit row, the other leaves
    a 500 and nothing.
    """
    restante = presupuesto_restante()
    if restante is None:
        return timeout

    if restante <= 0:
        raise TransientError(
            'No quedaba tiempo en esta petición para consultar el servicio '
            'externo. Inténtelo de nuevo.'
        )

    # Only a plain number is trimmed. ``None`` means the caller said nothing
    # and httpx applies its own default; an ``httpx.Timeout`` carries a limit
    # per phase and trimming it correctly is the caller's business. Neither is
    # silently replaced by the whole remaining budget, which would make an
    # unspecified timeout *longer* than it is today.
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        return min(float(timeout), restante)
    return timeout


def cliente(**kwargs):
    """An ``httpx.Client`` that knows how this deployment reaches the internet.

    Every outbound call in the application builds its client here, so turning a
    proxy on is one setting rather than a search through the code for the
    places that open sockets, and no call can ask for more time than the
    request it belongs to has left.
    """
    recortado = _dentro_del_presupuesto(kwargs.get('timeout'))
    # Sin «timeout» explícito no se toca nada: httpx tiene su propio valor por
    # defecto y ponerle aquí el presupuesto entero lo alargaría.
    if recortado is not None:
        kwargs['timeout'] = recortado

    proxy = proxy_configurado()
    if not proxy:
        # No proxy in the panel: httpx reads HTTPS_PROXY and NO_PROXY itself,
        # which is the behaviour somebody setting those variables expects.
        return httpx.Client(**kwargs)

    # Once we route, we route everything: with `trust_env` left on, the
    # «direct» transport picks up HTTPS_PROXY by itself, and internal traffic
    # goes out through the environment's proxy while the code says it is going
    # direct. The certificate bundle is read here instead, so a corporate CA
    # keeps working.
    verify = kwargs.pop('verify', _bundle_de_certificados())
    enrutador = _Enrutador(
        directo=httpx.HTTPTransport(verify=verify, trust_env=False),
        por_proxy=httpx.HTTPTransport(verify=verify, trust_env=False, proxy=proxy),
    )
    kwargs.setdefault('mounts', {'all://': enrutador})
    # And on the client too. It builds its own mounts from the environment
    # when `trust_env` is on, keyed by scheme -- «https://» is more specific
    # than our «all://» and quietly wins, so the proxy an administrator set in
    # the panel would be overridden by a variable nobody remembered.
    kwargs.setdefault('trust_env', False)
    return httpx.Client(**kwargs)


def descripcion():
    """How outbound traffic is leaving, for the administration screen."""
    proxy = proxy_configurado()
    if proxy:
        return f'A través del proxy configurado ({_sin_credenciales(proxy)}).'

    import os

    del_entorno = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    if del_entorno:
        return f'A través del proxy del entorno ({_sin_credenciales(del_entorno)}).'
    return 'Directamente, sin proxy.'


def _sin_credenciales(url):
    """A proxy URL safe to show: the password in it is a password.

    ``http://usuario:secreto@proxy:3128`` is a normal way to write one, and it
    would otherwise reach a screen and the logs.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        partes = urlsplit(str(url))
    except ValueError:
        return '(ilegible)'

    if not partes.hostname:
        return str(url)

    autoridad = partes.hostname
    if partes.port:
        autoridad = f'{autoridad}:{partes.port}'
    if partes.username:
        autoridad = f'{partes.username}:***@{autoridad}'

    return urlunsplit((partes.scheme, autoridad, partes.path, '', ''))
