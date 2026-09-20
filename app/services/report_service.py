"""Numbers about the trips one person may see.

Every query here starts from ``visible_trips_query``. That is not politeness:
an aggregate is a leak like any other, and «12 viajes en curso» told to someone
who may see three of them says something about the other nine. The scoping is
the query rather than a check afterwards, so a new report cannot forget it.

What is counted was chosen by what a manager does next. «Cuántas alertas hay»
is a number to feel bad about; «qué regla salta más» is a number to act on,
because a rule that fires on half the trips is usually a process problem rather
than fifty separate mistakes.
"""
import logging

from app.extensions import db
from app.models.alert import Alert
from app.models.document import Document
from app.models.enums import (
    ALERT_OPEN_STATES,
    DOCUMENT_TERMINAL_STATES,
    AlertSeverity,
    DocumentProcessState,
    TripStatus,
)
from app.models.itinerary import Accommodation, TravelSegment
from app.models.trip import Trip
from app.services.authorization_service import visible_trips_query
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Longer than this waiting for review and it is not a queue, it is a pile.
DIAS_ATASCADO = 3


def _ids_visibles(actor):
    """The trips this actor may see, as a subquery for everything else."""
    return db.select(visible_trips_query(actor).with_entities(Trip.id).subquery().c.id)


def viajes_por_estado(actor):
    """How the trips are distributed, in the states' own order."""
    visibles = _ids_visibles(actor)
    cuentas = dict(
        db.session.query(Trip.estado, db.func.count(Trip.id))
        .filter(Trip.id.in_(visibles))
        .group_by(Trip.estado)
        .all()
    )
    return [
        {'clave': str(estado), 'etiqueta': estado.label,
         'css': estado.css_class, 'total': cuentas.get(str(estado), 0)}
        for estado in TripStatus
    ]


def alertas_por_severidad(actor):
    """Open alerts by how much they matter."""
    visibles = _ids_visibles(actor)
    cuentas = dict(
        db.session.query(Alert.severidad, db.func.count(Alert.id))
        .filter(
            Alert.trip_id.in_(visibles),
            Alert.estado.in_([str(s) for s in ALERT_OPEN_STATES]),
        )
        .group_by(Alert.severidad)
        .all()
    )
    return [
        {'clave': str(sev), 'etiqueta': sev.label, 'total': cuentas.get(str(sev), 0)}
        for sev in AlertSeverity
    ]


def alertas_por_regla(actor, limite=8):
    """Which rules fire most.

    The report worth having: a rule that fires on half the trips is usually
    telling you something about how they are being prepared, not about fifty
    separate mistakes.
    """
    visibles = _ids_visibles(actor)
    filas = (
        db.session.query(Alert.regla, db.func.count(Alert.id).label('total'))
        .filter(
            Alert.trip_id.in_(visibles),
            Alert.estado.in_([str(s) for s in ALERT_OPEN_STATES]),
        )
        .group_by(Alert.regla)
        .order_by(db.desc('total'))
        .limit(limite)
        .all()
    )
    return [{'regla': regla, 'total': total} for regla, total in filas]


def documentos_por_estado(actor):
    """Where the documents are in the pipeline, worst first."""
    visibles = _ids_visibles(actor)
    cuentas = dict(
        db.session.query(Document.estado_proceso, db.func.count(Document.id))
        .filter(Document.trip_id.in_(visibles), Document.is_deleted.is_(False))
        .group_by(Document.estado_proceso)
        .all()
    )
    return [
        {'clave': str(estado), 'etiqueta': estado.label,
         'total': cuentas.get(str(estado), 0),
         'terminal': estado in DOCUMENT_TERMINAL_STATES}
        for estado in DocumentProcessState
        if cuentas.get(str(estado), 0)
    ]


def documentos_atascados(actor, dias=DIAS_ATASCADO, limite=10):
    """Documents that have been waiting long enough to be forgotten.

    Neither failed nor finished: sitting in a state that needs a person, for
    long enough that nobody is going to notice them on their own.
    """
    from datetime import timedelta

    visibles = _ids_visibles(actor)
    limite_tiempo = utcnow() - timedelta(days=dias)

    return (
        Document.query.filter(
            Document.trip_id.in_(visibles),
            Document.is_deleted.is_(False),
            Document.estado_proceso.notin_(
                [str(e) for e in DOCUMENT_TERMINAL_STATES]
                + [str(DocumentProcessState.APROBADO)]
            ),
            Document.updated_at < limite_tiempo,
        )
        .order_by(Document.updated_at.asc())
        .limit(limite)
        .all()
    )


def calidad_de_la_extraccion(actor):
    """How much of the itinerary still carries a doubt.

    The number that says whether automatic extraction is earning its keep: not
    how many documents were read, but how much of what it produced a person
    still has to check.
    """
    visibles = _ids_visibles(actor)
    resumen = {}

    for nombre, modelo in (('tramos', TravelSegment), ('alojamientos', Accommodation)):
        total = modelo.query.filter(
            modelo.trip_id.in_(visibles), modelo.is_deleted.is_(False),
        ).count()
        por_revisar = modelo.query.filter(
            modelo.trip_id.in_(visibles),
            modelo.is_deleted.is_(False),
            modelo.requiere_revision.is_(True),
        ).count()
        de_documento = modelo.query.filter(
            modelo.trip_id.in_(visibles),
            modelo.is_deleted.is_(False),
            modelo.documento_origen_id.isnot(None),
        ).count()

        resumen[nombre] = {
            'total': total,
            'por_revisar': por_revisar,
            'de_documento': de_documento,
            'a_mano': total - de_documento,
            'porcentaje_por_revisar': round(por_revisar * 100 / total) if total else 0,
        }

    return resumen


def viajes_por_mes(actor, meses=6):
    """Trips starting per month, oldest first.

    Grouped in Python rather than in SQL: the date functions differ between
    PostgreSQL and the SQLite the tests run on, and a report is not worth a
    dialect branch.
    """
    from datetime import timedelta

    visibles = _ids_visibles(actor)
    desde = utcnow() - timedelta(days=31 * meses)

    filas = (
        db.session.query(Trip.inicio_utc)
        .filter(
            Trip.id.in_(visibles),
            Trip.inicio_utc.isnot(None),
            Trip.inicio_utc >= desde,
        )
        .all()
    )

    cuentas = {}
    for (inicio,) in filas:
        clave = f'{inicio.year:04d}-{inicio.month:02d}'
        cuentas[clave] = cuentas.get(clave, 0) + 1

    return [{'mes': mes, 'total': total} for mes, total in sorted(cuentas.items())]


def informe_completo(actor):
    """Everything the reports page shows, in one call."""
    return {
        'viajes_por_estado': viajes_por_estado(actor),
        'alertas_por_severidad': alertas_por_severidad(actor),
        'alertas_por_regla': alertas_por_regla(actor),
        'documentos_por_estado': documentos_por_estado(actor),
        'documentos_atascados': documentos_atascados(actor),
        'calidad': calidad_de_la_extraccion(actor),
        'viajes_por_mes': viajes_por_mes(actor),
        'dias_atascado': DIAS_ATASCADO,
    }
