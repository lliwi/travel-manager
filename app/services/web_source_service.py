"""The allow-list of public sources, as an administrator edits it.

``web_research_service`` only ever fetches a domain listed here, so this is the
one place that list can grow. The rules below are what keep an edit from
quietly widening it into something else.
"""
import ipaddress
import re
import uuid
from urllib.parse import urlparse

from app.extensions import db
from app.models.advisory import WebFetch, WebSource
from app.models.enums import AdvisoryCategory, AuditResourceType
from app.services import audit_service
from app.utils.errors import ConflictError, ResourceNotFound, ValidationError

_ETIQUETA = re.compile(r'^(?!-)[a-z0-9-]{1,63}(?<!-)$')


def listar():
    return WebSource.query.order_by(
        WebSource.activa.desc(), WebSource.es_oficial.desc(),
        WebSource.prioridad.asc(), WebSource.dominio.asc(),
    ).all()


def get_or_404(source_id):
    try:
        clave = uuid.UUID(str(source_id))
    except ValueError:
        clave = None
    fuente = db.session.get(WebSource, clave) if clave else None
    if fuente is None:
        raise ResourceNotFound('La fuente indicada no existe.')
    return fuente


def normalizar_dominio(texto):
    """The bare host an administrator meant, or a refusal saying why.

    Pasting a whole URL is the common case, so the scheme and path are
    dropped rather than refused. What is refused is anything that would make
    the entry mean more than one site: a wildcard, a bare TLD, an IP address.
    A subdomain of an entry is already accepted by the research service, so
    ``*.gob.es`` is never needed -- and ``gob.es`` alone would let in every
    Spanish ministry at once.
    """
    crudo = (texto or '').strip().lower()
    if '://' in crudo:
        crudo = urlparse(crudo).hostname or ''
    crudo = crudo.split('/')[0].split(':')[0].rstrip('.')
    if crudo.startswith('www.'):
        crudo = crudo[4:]

    if not crudo:
        raise ValidationError('Indique un dominio, p. ej. «exteriores.gob.es».')
    if '*' in crudo:
        raise ValidationError(
            'Sin comodines: un dominio ya incluye sus subdominios.'
        )
    try:
        ipaddress.ip_address(crudo.strip('[]'))
    except ValueError:
        pass
    else:
        raise ValidationError('Indique un nombre de dominio, no una dirección IP.')

    etiquetas = crudo.split('.')
    if len(etiquetas) < 2 or not all(_ETIQUETA.match(e) for e in etiquetas):
        raise ValidationError(f'«{texto}» no es un nombre de dominio válido.')
    return crudo


def _comprobar_url_base(url_base, dominio):
    """A start page on another site would be fetched with this one's trust."""
    if not url_base:
        return None
    url_base = url_base.strip()
    partes = urlparse(url_base)
    host = (partes.hostname or '').lower().rstrip('.')
    if partes.scheme not in ('https', 'http') or not host:
        raise ValidationError('La URL de inicio debe empezar por https://.')
    if host != dominio and not host.endswith(f'.{dominio}'):
        raise ValidationError(
            f'La URL de inicio tiene que estar en {dominio}; si no, se '
            'consultaría otro sitio en nombre de este.'
        )
    return url_base


def _aplicar(fuente, datos):
    dominio = normalizar_dominio(datos.get('dominio'))
    otra = WebSource.query.filter(
        WebSource.dominio == dominio, WebSource.id != fuente.id,
    ).first()
    if otra is not None:
        raise ConflictError(f'«{dominio}» ya está en la lista como «{otra.nombre}».')

    nombre = (datos.get('nombre') or '').strip()
    if not nombre:
        raise ValidationError('Indique un nombre.')

    fuente.nombre = nombre[:250]
    fuente.dominio = dominio
    fuente.url_base = _comprobar_url_base(datos.get('url_base'), dominio)
    fuente.categoria = AdvisoryCategory.coerce(
        datos.get('categoria'), AdvisoryCategory.SEGURIDAD,
    )
    fuente.es_oficial = bool(datos.get('es_oficial'))
    fuente.pais_codigo = (datos.get('pais_codigo') or '').strip().upper()[:2] or None
    fuente.idioma = (datos.get('idioma') or '').strip().lower()[:10] or None
    fuente.activa = bool(datos.get('activa'))
    fuente.prioridad = int(datos.get('prioridad') or 100)
    fuente.confianza_base = datos.get('confianza_base')
    fuente.notas = (datos.get('notas') or '').strip() or None


def crear(actor, datos):
    fuente = WebSource()
    _aplicar(fuente, datos)
    db.session.add(fuente)
    db.session.commit()
    _auditar('web_source.created', fuente, actor)
    return fuente


def actualizar(actor, fuente, datos):
    antes = fuente.to_dict()
    _aplicar(fuente, datos)
    db.session.commit()
    despues = fuente.to_dict()
    cambios = {k: [antes[k], despues[k]] for k in despues if antes.get(k) != despues[k]}
    _auditar('web_source.updated', fuente, actor, cambios=cambios)
    return fuente


def cambiar_estado(actor, fuente, activa):
    fuente.activa = bool(activa)
    db.session.commit()
    _auditar('web_source.activated' if activa else 'web_source.deactivated',
             fuente, actor)
    return fuente


def eliminar(actor, fuente):
    """Delete a source, only when nothing would be lost or come back.

    A source that has been consulted is what the stored pages and the
    advisories built on them cite. A seeded one is put back by ``flask
    seed``, which runs on every start. Both are switched off instead.
    """
    if es_de_serie(fuente):
        raise ConflictError(
            'Es una fuente de las que trae la aplicación y volvería en el '
            'próximo arranque. Desactívela en su lugar.'
        )
    if WebFetch.query.filter_by(web_source_id=fuente.id).first() is not None:
        raise ConflictError(
            'Ya se ha consultado y hay recomendaciones que la citan. '
            'Desactívela en su lugar.'
        )
    _auditar('web_source.deleted', fuente, actor)
    db.session.delete(fuente)
    db.session.commit()


def es_de_serie(fuente):
    from app.services.seed_service import WEB_SOURCE_DEFINITIONS

    return any(d[1] == fuente.dominio for d in WEB_SOURCE_DEFINITIONS)


def _auditar(accion, fuente, actor, **extra):
    audit_service.record(
        accion,
        recurso_tipo=AuditResourceType.CONFIGURACION,
        recurso_id=str(fuente.id),
        actor=actor,
        metadatos={'dominio': fuente.dominio, 'nombre': fuente.nombre, **extra},
    )
