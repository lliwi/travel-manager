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
        content, title, status, content_type, size = _do_fetch(url)
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
    return _as_result(record)


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
                _to_text(text), title, response.status_code, content_type, len(raw)
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
