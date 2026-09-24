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
from decimal import Decimal

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
from app.utils.timeutil import as_aware_utc, utcnow

logger = logging.getLogger(__name__)

#: Longer than this waiting for review and it is not a queue, it is a pile.
DIAS_ATASCADO = 3

#: Trips with no project are grouped here rather than dropped: the field is
#: optional, and a total that leaves them out does not add up to what anybody
#: else would compute.
SIN_PROYECTO = 'Sin proyecto'


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


#: Meses hacia atrás y hacia delante que cubre el informe de gasto. A
#: diferencia de los demás paneles, este no mira solo lo ocurrido: en gestión
#: de viajes casi todo el gasto está comprometido antes de suceder, y una tabla
#: que acaba en el mes en curso deja fuera justo lo que hay que presupuestar.
#: De ahí el reparto: uno atrás, para cerrar el mes que acaba de pasar, y
#: cuatro por delante, que es donde están los viajes que aún se pueden mover.
MESES_ATRAS = 1
MESES_ADELANTE = 4


def gastos_por_proyecto(actor, atras=MESES_ATRAS, adelante=MESES_ADELANTE):
    """What each project spent, month by month.

    Costs are the one figure on this page that is not available to everybody:
    they sit behind the ``COSTES_HABILITADOS`` switch and the ``VER_COSTES``
    permission, which the ``usuario`` role never holds. Asked by somebody
    without it, this returns None rather than zeros -- «todos los proyectos a
    cero» is a different statement from «esto no es para usted», and the second
    is the true one.

    Trips with no project are grouped under one row rather than dropped: the
    field is optional, and a total that silently leaves them out does not add
    up to what anybody else would compute.

    What a trip cost is ``Trip.coste_total`` -- the sum of what its bookings
    say -- and ``coste_estimado`` only while no booking carries an amount. The
    two are never added: one is the bill and the other the plan.
    """
    from app.services.authorization_service import Permiso, permisos_de

    if Permiso.VER_COSTES not in permisos_de(actor):
        return None

    visibles = _ids_visibles(actor)
    desde = _inicio_de_mes(utcnow(), atras)
    etiquetas = _meses_desde(desde, atras + 1 + adelante)
    hasta = _inicio_de_mes(utcnow(), -(adelante + 1))

    # Whole trips rather than one column: what a trip costs is
    # ``Trip.coste_total`` -- the sum of what its bookings say -- and reading
    # ``coste_estimado`` alone missed every trip whose amounts came out of a
    # document, which is most of them. One definition, in the model, used here.
    viajes = (
        Trip.query.filter(
            Trip.id.in_(visibles),
            Trip.is_deleted.is_(False),
            Trip.inicio_utc.isnot(None),
            Trip.inicio_utc >= desde,
            Trip.inicio_utc < hasta,
        )
        .all()
    )

    proyectos, monedas = {}, set()
    estimados = 0

    for viaje in viajes:
        mes = f'{as_aware_utc(viaje.inicio_utc):%Y-%m}'
        if mes not in etiquetas:
            continue

        # The bill first, the estimate only while there is no bill: one is what
        # the bookings say and the other what somebody expected, and adding
        # both would count the same trip twice.
        importe = viaje.coste_total
        if importe is None:
            importe = float(viaje.coste_estimado) if viaje.coste_estimado else None
            if importe is not None:
                estimados += 1

        clave = (viaje.proyecto or '').strip() or SIN_PROYECTO
        entrada = proyectos.setdefault(
            clave, {'por_mes': {m: Decimal(0) for m in etiquetas}, 'sin_importe': 0},
        )

        # A project whose trips carry no figure still gets its row, at zero.
        # Dropping it made «no aparece» indistinguishable from «no tiene
        # datos», and the first reads as a fault in the report.
        if not importe:
            entrada['sin_importe'] += 1
            continue

        entrada['por_mes'][mes] += Decimal(str(importe))
        monedas.update(viaje.monedas_del_itinerario)
        if viaje.moneda:
            monedas.add(viaje.moneda.upper())

    orden = sorted(
        proyectos.items(),
        key=lambda par: (par[0] == SIN_PROYECTO,
                         -sum(par[1]['por_mes'].values()), par[0]),
    )

    return {
        'meses': etiquetas,
        # Several currencies in one column do not add up, so the screen is told
        # rather than shown a total that means nothing.
        'moneda': monedas.pop() if len(monedas) == 1 else None,
        'monedas_mezcladas': len(monedas) > 1,
        # How many of the figures are still somebody's estimate rather than
        # what a booking says, so the screen can qualify the total instead of
        # presenting a plan as a bill.
        'estimados': estimados,
        'proyectos': [
            {
                'nombre': nombre,
                'por_mes': [float(datos['por_mes'][m]) for m in etiquetas],
                'total': float(sum(datos['por_mes'].values())),
                'sin_importe': datos['sin_importe'],
            }
            for nombre, datos in orden
        ],
        'total': float(sum(sum(d['por_mes'].values()) for d in proyectos.values())),
        'sin_importe': sum(d['sin_importe'] for d in proyectos.values()),
    }


def _inicio_de_mes(momento, atras=0):
    """The first instant of the month ``atras`` months before ``momento``.

    A negative ``atras`` moves forward, which is how the upper bound of the
    window is expressed without a second function that would drift from this
    one.
    """
    anio, mes = momento.year, momento.month - atras
    while mes <= 0:
        mes += 12
        anio -= 1
    while mes > 12:
        mes -= 12
        anio += 1
    return momento.replace(year=anio, month=mes, day=1, hour=0, minute=0,
                           second=0, microsecond=0)


def _meses_desde(inicio, cuantos):
    """``['2026-04', '2026-05', ...]``, in order."""
    etiquetas, anio, mes = [], inicio.year, inicio.month
    for _ in range(cuantos):
        etiquetas.append(f'{anio:04d}-{mes:02d}')
        mes += 1
        if mes > 12:
            mes, anio = 1, anio + 1
    return etiquetas


def informe_completo(actor):
    """Everything the reports page shows, in one call."""
    return {
        'viajes_por_estado': viajes_por_estado(actor),
        'alertas_por_severidad': alertas_por_severidad(actor),
        'alertas_por_regla': alertas_por_regla(actor),
        'documentos_por_estado': documentos_por_estado(actor),
        'documentos_atascados': documentos_atascados(actor),
        'calidad': calidad_de_la_extraccion(actor),
        'gastos_por_proyecto': gastos_por_proyecto(actor),
        'viajes_por_mes': viajes_por_mes(actor),
        'dias_atascado': DIAS_ATASCADO,
    }
