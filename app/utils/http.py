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
"""

import logging

import httpx

logger = logging.getLogger(__name__)

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


def _con_corchetes(host):
    """An IPv6 literal needs brackets to survive being parsed as a URL."""
    if ':' in host and not host.startswith('['):
        return f'[{host}]'
    return host


def _mounts():
    """Bypass rules in the shape httpx wants them.

    ``None`` as the transport for a pattern means «no proxy for this», which is
    how httpx spells an exception once a proxy has been given explicitly: the
    ``NO_PROXY`` variable is only consulted for proxies that came from the
    environment.
    """
    return {f'all://{_con_corchetes(host)}': None for host in excepciones()}


def cliente(**kwargs):
    """An ``httpx.Client`` that knows how this deployment reaches the internet.

    Every outbound call in the application builds its client here, so turning
    a proxy on is one setting rather than a search through the code for the
    places that open sockets.
    """
    proxy = proxy_configurado()
    if not proxy:
        # No proxy in the panel: httpx reads HTTPS_PROXY and NO_PROXY itself,
        # which is the behaviour somebody setting those variables expects.
        return httpx.Client(**kwargs)

    kwargs.setdefault('mounts', _mounts())
    return httpx.Client(proxy=proxy, **kwargs)


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
