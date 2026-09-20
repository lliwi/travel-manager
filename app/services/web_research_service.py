"""Public information retrieval (specification sections 2.6 and 3.2).

Three controls, all mandatory:

* **Allow-list.** Only domains in ``web_sources`` are fetched. There is no
  "fetch this arbitrary URL" path, because there is no legitimate need for one
  and it is the shortest route to SSRF.
* **Address validation.** Every resolved IP is checked before connecting, and
  again on each redirect, so a permitted hostname that resolves to a private
  address or to the cloud metadata endpoint is refused.
* **Sanitisation.** Retrieved content is stripped to text and stored as
  untrusted data. It never reaches a model as an instruction.
"""
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from flask import current_app

from app.extensions import db
from app.models.advisory import WebFetch, WebSource
from app.models.enums import AdvisoryCategory, AuditResourceType
from app.services import audit_service
from app.utils.errors import SSRFBlocked, WebResearchError
from app.utils.hashing import sha256_bytes
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Only these schemes are ever fetched.
ALLOWED_SCHEMES = ('https', 'http')

#: Only these content types are parsed.
ALLOWED_CONTENT_TYPES = ('text/html', 'text/plain', 'application/xhtml+xml')

#: Never followed, whatever the allow-list says. The metadata endpoints are
#: called out explicitly because reaching one is the classic SSRF payoff.
BLOCKED_HOSTS = frozenset({
    'localhost', 'metadata.google.internal', 'metadata.goog',
    'instance-data', '169.254.169.254', 'metadata',
})


def is_safe_address(hostname):
    """Resolve a hostname and refuse anything that is not a public address.

    Returns ``(ok, motivo)``. Checking the *resolved* address rather than the
    name is the point: an allow-listed domain whose DNS record points at
    169.254.169.254 must still be refused.
    """
    if not hostname:
        return False, 'sin nombre de host'

    lowered = hostname.strip().lower().rstrip('.')
    if lowered in BLOCKED_HOSTS:
        return False, f'host bloqueado: {lowered}'

    try:
        infos = socket.getaddrinfo(lowered, None)
    except socket.gaierror as exc:
        return False, f'no se pudo resolver: {exc}'

    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False, f'dirección no válida: {address}'

        if (parsed.is_private or parsed.is_loopback or parsed.is_link_local
                or parsed.is_reserved or parsed.is_multicast
                or parsed.is_unspecified):
            return False, f'dirección no pública: {address}'

    return True, 'dirección pública'


def allowed_source(url):
    """The ``WebSource`` governing a URL, or None when it is not allow-listed.

    Matching is on the registrable host, and a subdomain of an allow-listed
    domain is accepted -- ``www.exteriores.gob.es`` for ``exteriores.gob.es`` --
    but a domain that merely *ends with* one is not: ``evilexteriores.gob.es``
    must not pass.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        return None

    host = (parsed.hostname or '').lower().rstrip('.')
    if not host:
        return None

    for source in WebSource.query.filter_by(activa=True).all():
        domain = source.dominio.lower().strip().rstrip('.')
        if host == domain or host.endswith(f'.{domain}'):
            return source
    return None


def fetch(url, source=None, use_cache=True, actor=None):
    """Fetch one allow-listed URL, with caching and SSRF checks.

    Returns:
        A dict with ``url``, ``titulo``, ``contenido``, ``fuente``,
        ``es_oficial`` and ``consultado_en``.
    """
    if not current_app.config['WEB_RESEARCH_ENABLED']:
        raise WebResearchError('La búsqueda de información pública está desactivada.')

    source = source or allowed_source(url)
    if source is None:
        raise SSRFBlocked(
            'El destino solicitado no está en la lista de fuentes autorizadas.'
        )

    url_hash = sha256_bytes(_canonical(url))

    if use_cache:
        cached = (
            WebFetch.query.filter_by(url_hash=url_hash)
            .order_by(WebFetch.created_at.desc())
            .first()
        )
        if cached is not None and cached.is_fresh and not cached.error:
            return _as_result(cached)

    parsed = urlparse(url)
    safe, reason = is_safe_address(parsed.hostname)
    if not safe:
        _record_block(url, source, reason, actor)
        raise SSRFBlocked(f'El destino solicitado no está permitido ({reason}).')

    record = WebFetch(
        web_source_id=source.id,
        url=url[:2000],
        url_hash=url_hash,
    )

    try:
        content, title, status, content_type, size, html = _do_fetch(url)
        record.titulo = title
        record.contenido = content
        record.status_code = status
        record.content_type = content_type
        record.bytes_descargados = size
        record.expira_en = _expiry()
    except SSRFBlocked:
        raise
    except Exception as exc:
        record.error = str(exc)[:500]
        db.session.add(record)
        db.session.commit()
        raise WebResearchError(f'No se pudo consultar la fuente: {exc}') from exc

    db.session.add(record)
    db.session.commit()

    audit_service.record(
        'research.fetched',
        recurso_tipo=AuditResourceType.RECOMENDACION,
        recurso_id=str(record.id),
        actor=actor,
        metadatos={'url': url[:300], 'fuente': source.nombre, 'status': status},
    )
    resultado = _as_result(record)
    # Kept for the caller that wants to follow a link out of this page; it is
    # not stored, because the text is what a model ever reads.
    resultado['_html'] = html
    return resultado


def _do_fetch(url):
    """Perform the request, validating every redirect hop.

    Redirects are followed manually: letting the client follow them would skip
    the address check on the destination, which is how an allow-listed domain
    becomes a redirect to an internal service.
    """
    timeout = current_app.config['WEB_FETCH_TIMEOUT']
    max_bytes = current_app.config['WEB_FETCH_MAX_BYTES']
    current = url

    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers={
            'User-Agent': 'TravelManager/1.0 (gestor de viajes corporativo)',
            'Accept': 'text/html,application/xhtml+xml,text/plain',
            'Accept-Language': 'es,en;q=0.8',
        },
    ) as client:
        for _ in range(5):
            parsed = urlparse(current)
            safe, reason = is_safe_address(parsed.hostname)
            if not safe:
                raise SSRFBlocked(f'Redirección a un destino no permitido ({reason}).')
            if allowed_source(current) is None:
                raise SSRFBlocked(
                    'La redirección apunta a un dominio que no está autorizado.'
                )

            response = client.get(current)

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('location')
                if not location:
                    break
                current = urljoin(current, location)
                continue

            response.raise_for_status()

            content_type = (response.headers.get('content-type') or '').split(';')[0].strip()
            if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                raise WebResearchError(
                    f'El tipo de contenido «{content_type}» no se procesa.'
                )

            raw = response.content[:max_bytes]
            text = raw.decode(response.encoding or 'utf-8', errors='replace')
            title = _extract_title(text)
            return (
                _to_text(text), title, response.status_code, content_type,
                len(raw), text,
            )

        raise WebResearchError('Demasiadas redirecciones.')


def search(consulta, categoria=None, trip=None, actor=None, limit=4):
    """Consult the allow-listed sources relevant to a query.

    Phase 1 has no search-engine connector: the sources are the configured entry
    points, ranked by whether they are official, by the destination country and
    by their priority. Section 2.6 calls for assisted consultation, which this
    is; it books nothing and buys nothing.
    """
    if not current_app.config['WEB_RESEARCH_ENABLED']:
        return []

    query = WebSource.query.filter_by(activa=True)
    if categoria:
        category = AdvisoryCategory.coerce(categoria)
        if category is not None:
            query = query.filter(WebSource.categoria == str(category))

    sources = query.order_by(
        WebSource.es_oficial.desc(), WebSource.prioridad.asc()
    ).all()

    countries = set(trip.paises) if trip is not None else set()
    if countries:
        # A source tied to a country the trip does not visit is noise.
        sources = [
            s for s in sources
            if not s.pais_codigo or s.pais_codigo in countries or s.es_oficial
        ]

    results = []
    for source in sources[:limit]:
        if not source.url_base:
            continue
        try:
            results.append(fetch(source.url_base, source=source, actor=actor))
        except (SSRFBlocked, WebResearchError) as exc:
            logger.warning('No se pudo consultar %s: %s', source.dominio, exc)
            continue

    return results


def _record_block(url, source, reason, actor):
    """Audit a refused fetch. An SSRF attempt must leave a trace."""
    audit_service.record_denied(
        'research.blocked',
        recurso_tipo=AuditResourceType.RECOMENDACION,
        actor=actor,
        motivo=f'{reason} — {url[:200]}',
    )


def _as_result(record):
    return {
        'url': record.url,
        'titulo': record.titulo,
        'contenido': record.contenido or '',
        'resumen': record.resumen,
        'fuente': record.source.nombre if record.source else None,
        'es_oficial': record.source.es_oficial if record.source else False,
        'consultado_en': record.created_at.isoformat() if record.created_at else None,
    }


def _expiry():
    from datetime import timedelta


    hours = current_app.config['WEB_FETCH_CACHE_HOURS']
    return utcnow() + timedelta(hours=hours)


def _canonical(url):
    """Normalise a URL for cache keying."""
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    path = parsed.path.rstrip('/') or '/'
    return f'{parsed.scheme}://{host}{path}?{parsed.query}'


_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
_TAG_RE = re.compile(r'<[^>]+>')


def _extract_title(html):
    match = _TITLE_RE.search(html)
    if not match:
        return None
    import html as html_module

    return html_module.unescape(_TAG_RE.sub('', match.group(1))).strip()[:500]


def _to_text(html):
    """Reduce HTML to plain text.

    The content is stored as text, never as markup: retrieved HTML is untrusted,
    and keeping it as markup invites it being rendered somewhere later.
    """
    import html as html_module

    text = re.sub(r'(?is)<(script|style|noscript|svg).*?</\1>', ' ', html)
    text = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>', '\n', text)
    text = _TAG_RE.sub(' ', text)
    text = html_module.unescape(text)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
    return text.strip()


#: How much of a page to hand the model, and how much context to keep around
#: the place it actually mentions.
FRAGMENTO_MAX = 6000
_VENTANA = 2500


def fragmento_sobre(texto, lugares, maximo=FRAGMENTO_MAX):
    """The part of a page that talks about a place, not its first page-worth.

    An official source often answers with an index of every country. Taking its
    first characters hands the model the top of an alphabetical list: asked
    about London, it read about Afghanistan and wrote an advisory about
    Afghanistan, which we then filed under the United Kingdom.

    Returns None when the page never mentions the place. A source that does not
    talk about the destination has nothing to contribute, and saying so is
    better than sending a fragment that happens to be about somewhere else.
    """
    if not texto:
        return None

    nombres = [
        str(lugar).strip() for lugar in (lugares or [])
        if lugar and str(lugar).strip()
    ]
    if not nombres:
        return texto[:maximo]

    plano = texto.lower()
    posiciones = []
    for nombre in nombres:
        desde = 0
        aguja = nombre.lower()
        while len(posiciones) < 12:
            encontrado = plano.find(aguja, desde)
            if encontrado < 0:
                break
            posiciones.append(encontrado)
            desde = encontrado + len(aguja)

    if not posiciones:
        return None

    if len(texto) <= maximo:
        return texto

    inicio = max(0, min(posiciones) - 400)
    return texto[inicio:inicio + maximo]


#: Anything shorter is not a place name worth matching a URL against: «GB» or
#: «ES» appear inside half the paths on a site.
_LONGITUD_MINIMA_LUGAR = 4

_URLS_RE = re.compile(r'["\'](\/[^"\'<>\s]{4,300}|https?:\/\/[^"\'<>\s]{4,300})["\']')


def _normalizar(texto):
    """Lowercase, unaccented, with every separator read as a space.

    A site writes the same country as «Reino+Unido», «Reino%20Unido» or
    «united-kingdom», and the punctuation between the words is the site's
    convention, not part of the name. Comparing «united kingdom» against a
    hyphenated path found nothing at all.
    """
    import unicodedata
    from urllib.parse import unquote

    plano = unquote(str(texto or ''))
    descompuesto = unicodedata.normalize('NFKD', plano)
    sin_tildes = ''.join(c for c in descompuesto if not unicodedata.combining(c))
    return ' '.join(re.split(r'[^0-9a-zA-Z]+', sin_tildes.lower())).strip()


def enlace_al_lugar(html, base_url, nombres):
    """The link on this page that leads to a given place's own page.

    An official source answers with an index of every country and a link to
    each one's page; summarising the index tells a traveller about the
    alphabet, not about where they are going. Following the link is what puts
    the destination's own text in front of the model.

    The link is *discovered*, never composed: guessing that a site files the
    United Kingdom under some invented path would be a URL we made up, and it
    would rot the first time the site changed. Here the page is read for a URL
    that names the place, whatever shape that site gives it -- these ones live
    inside a JSON payload rather than in an anchor, so the whole document is
    searched rather than its links.

    Returns an absolute URL within an allow-listed source, or None.
    """
    candidatos = [n for n in (nombres or [])
                  if n and len(str(n)) >= _LONGITUD_MINIMA_LUGAR]
    if not html or not candidatos:
        return None

    # The front page's own directory. A site has many pages naming a place --
    # «Reino Unido» is also an embassy, a consulate and a trade office -- and
    # the one we came for is the sibling of the page we were sent to, not the
    # shortest URL that happens to mention it.
    seccion = base_url.rsplit('/', 1)[0] + '/'

    encontradas = [
        urljoin(base_url, u) for u in dict.fromkeys(_URLS_RE.findall(html))
    ]
    encontradas = [
        u for u in encontradas
        if u != base_url and allowed_source(u) is not None
    ]

    # Names in the order the caller gave them: a country names the page about
    # a country, while its capital also names an embassy.
    for nombre in candidatos:
        aguja = _normalizar(nombre)
        coinciden = [u for u in encontradas if aguja in _normalizar(u)]
        if not coinciden:
            continue
        return min(
            coinciden,
            key=lambda u: (not u.startswith(seccion), len(u)),
        )

    return None


def pagina_del_lugar(source, nombres, actor=None):
    """Fetch a source's page for a given place, falling back to its front page.

    Returns the fetch result, or None when the source cannot be consulted.
    """
    try:
        portada = fetch(source.url_base, source=source, actor=actor)
    except (SSRFBlocked, WebResearchError) as exc:
        logger.warning('No se pudo consultar %s: %s', source.dominio, exc)
        return None

    html = portada.pop('_html', None)
    if html is None:
        # The cache stores the text a model reads, not the markup the links
        # live in, so a cache hit leaves nothing to follow. Fetching the front
        # page again for that one purpose costs a request next to a model call,
        # and keeps the text cache doing its job.
        try:
            html = _do_fetch(source.url_base)[5]
        except (SSRFBlocked, WebResearchError) as exc:
            logger.info('No se pudo releer %s para buscar enlaces: %s',
                        source.dominio, exc)
            return portada

    enlace = enlace_al_lugar(html, source.url_base, nombres)
    if not enlace:
        return portada

    try:
        propia = fetch(enlace, actor=actor)
    except (SSRFBlocked, WebResearchError) as exc:
        logger.info('No se pudo seguir el enlace a %s: %s', enlace, exc)
        return portada

    propia.pop('_html', None)
    logger.info('Consultada la página de %s en %s.', nombres[0], source.dominio)
    return propia
