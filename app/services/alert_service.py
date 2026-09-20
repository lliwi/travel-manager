"""Alert reading and state management.

The engine itself lives in ``app.services.alerts``; this module is the
application-facing surface: listing alerts for an actor, changing their state
with a comment, and scheduling recalculations.
"""
import logging

from app.extensions import db
from app.models.alert import Alert, AlertTransition
from app.models.enums import (
    ALERT_OPEN_STATES,
    AlertState,
    AlertTrigger,
    AuditResourceType,
    RoleCode,
)
from app.services import audit_service
from app.utils.errors import ValidationError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)


def list_for_trip(actor, trip, estado=None, severidad=None, include_closed=True):
    """A trip's alerts, scoped to what the actor may see.

    A traveller sees trip-wide alerts and their own, never another traveller's.
    """
    query = Alert.query.filter(Alert.trip_id == trip.id)

    if not (actor.has_role(RoleCode.ADMINISTRADOR) or actor.has_role(RoleCode.GESTOR)):
        traveler = trip.traveler_for(actor.id)
        if traveler is None:
            return []
        query = query.filter(
            db.or_(
                Alert.trip_traveler_id.is_(None),
                Alert.trip_traveler_id == traveler.id,
            )
        )

    if estado:
        estados = estado if isinstance(estado, (list, tuple)) else [estado]
        query = query.filter(Alert.estado.in_([str(e) for e in estados]))
    elif not include_closed:
        query = query.filter(Alert.estado.in_([str(s) for s in ALERT_OPEN_STATES]))

    if severidad:
        severidades = severidad if isinstance(severidad, (list, tuple)) else [severidad]
        query = query.filter(Alert.severidad.in_([str(s) for s in severidades]))

    alerts = query.all()
    # Severity first, then most recent: a critical alert must never be below an
    # informational one just because it was generated earlier.
    alerts.sort(
        key=lambda a: (
            -(a.severidad.rank if a.severidad else 0),
            a.estado is not AlertState.ABIERTA,
            -(a.generada_en.timestamp() if a.generada_en else 0),
        )
    )
    return alerts


def count_open(actor, trip):
    """How many open alerts this actor can see on a trip."""
    return len(list_for_trip(actor, trip, estado=[s.value for s in ALERT_OPEN_STATES]))


def change_state(actor, alert, nuevo_estado, comentario=None, responsable_id=None,
                 commit=True):
    """Accept, resolve or dismiss an alert.

    Section 5.3 requires this to be possible *with a comment*, so the comment is
    preserved on the transition even if the alert later changes state again.
    """
    nuevo = AlertState.coerce(nuevo_estado)
    if nuevo is None:
        raise ValidationError('El estado de alerta indicado no es válido.')

    anterior = alert.estado
    if anterior is nuevo:
        return alert

    if anterior is AlertState.RESUELTA and alert.cierre_automatico is False:
        # Reopening a manually resolved alert is allowed but worth noting.
        logger.info('Reapertura de la alerta %s por %s', alert.id, actor.username)

    alert.estado = nuevo
    alert.comentario = comentario or alert.comentario

    if responsable_id:
        alert.responsable_id = responsable_id

    if nuevo in (AlertState.RESUELTA, AlertState.DESCARTADA):
        alert.resuelta_en = utcnow()
        alert.resuelta_por_id = actor.id
        alert.cierre_automatico = False
    elif nuevo is AlertState.ABIERTA:
        alert.resuelta_en = None
        alert.resuelta_por_id = None
        alert.cierre_automatico = False

    db.session.add(AlertTransition(
        alert_id=alert.id,
        estado_anterior=anterior,
        estado_nuevo=nuevo,
        comentario=comentario,
        actor_id=actor.id,
        automatico=False,
    ))

    if commit:
        db.session.commit()
        audit_service.record(
            'alert.state_changed',
            recurso_tipo=AuditResourceType.ALERTA,
            recurso_id=str(alert.id),
            actor=actor,
            metadatos={
                'trip_id': str(alert.trip_id),
                'tipo': alert.tipo,
                'anterior': str(anterior),
                'nuevo': str(nuevo),
            },
        )
    return alert


def assign(actor, alert, responsable_id, commit=True):
    """Assign responsibility for an alert."""
    alert.responsable_id = responsable_id
    if commit:
        db.session.commit()
        audit_service.record(
            'alert.assigned',
            recurso_tipo=AuditResourceType.ALERTA,
            recurso_id=str(alert.id),
            actor=actor,
            metadatos={'responsable_id': str(responsable_id)},
        )
    return alert


def recalculate(trip, trigger=AlertTrigger.MANUAL, actor=None):
    """Run the alert engine over a trip, synchronously."""
    from app.services.alerts import engine

    run = engine.run(trip, trigger=trigger, actor=actor)
    audit_service.record(
        'alerts.recalculated',
        recurso_tipo=AuditResourceType.VIAJE,
        recurso_id=str(trip.id),
        actor=actor,
        metadatos={
            'disparador': str(trigger),
            'creadas': run.creadas,
            'actualizadas': run.actualizadas,
            'cerradas': run.cerradas,
        },
    )

    _avisar_de_las_graves(trip)
    return run


def _avisar_de_las_graves(trip):
    """Tell the people on a trip about its serious open alerts.

    Only the serious ones, and only while open. An inbox that reports every
    informative finding is an inbox that gets muted, and then the one that
    mattered goes unread with the rest.

    Keyed by the alert, so the nightly recalculation finds yesterday's telling
    already there instead of repeating it.
    """
    from flask import url_for

    from app.models.enums import AlertSeverity, AlertState, NotificationKind
    from app.services import notification_service

    graves = [
        a for a in trip.alerts
        if a.estado is AlertState.ABIERTA
        and a.severidad in (AlertSeverity.ALTA, AlertSeverity.CRITICA)
    ]
    if not graves:
        return

    interesados = notification_service.interesados_en(trip)
    for alerta in graves:
        try:
            enlace = url_for('alerts.detail', alert_id=alerta.id)
        except Exception:
            enlace = None

        notification_service.notificar_a_varios(
            interesados,
            tipo=NotificationKind.ALERTA,
            clave=f'alerta:{alerta.id}',
            titulo=alerta.titulo,
            mensaje=alerta.mensaje,
            enlace=enlace,
            trip=trip,
            datos={'severidad': str(alerta.severidad)},
        )


def schedule_recalculation(trip_id, trigger=AlertTrigger.ITINERARIO, actor=None):
    """Queue a recalculation, falling back to running it inline.

    Called after anything that changes an itinerary. If the queue is unavailable
    the alerts are still computed, just synchronously -- stale alerts are worse
    than a slow request.
    """
    from app.tasks.dispatch import is_eager

    if is_eager():
        return _recalculate_inline(trip_id, trigger, actor)

    try:
        from app.tasks.alert_tasks import recalculate_trip_alerts

        return recalculate_trip_alerts.delay(
            str(trip_id), str(trigger), str(actor.id) if actor else None
        )
    except Exception:
        logger.warning(
            'No se pudo encolar el recálculo de alertas de %s; se ejecuta en línea.',
            trip_id,
        )
        return _recalculate_inline(trip_id, trigger, actor)


def _recalculate_inline(trip_id, trigger, actor):
    """Run the engine in this process."""
    from app.models.trip import Trip

    trip = trip_id if isinstance(trip_id, Trip) else db.session.get(Trip, trip_id)
    if trip is None:
        return None
    return recalculate(trip, trigger=trigger, actor=actor)


def render_evidence(alert):
    """Turn an alert's evidence into rows the template can display.

    Keeps presentation choices out of the Jinja template and makes the evidence
    of a new rule render sensibly without touching the view.
    """
    evidencia = alert.evidencia or {}
    rows = []

    labels = {
        'margen_minutos': 'Margen calculado',
        'margen_legible': 'Margen calculado',
        'umbral_minutos': 'Umbral aplicado',
        'umbral_motivo': 'Motivo del umbral',
        'solape_minutos': 'Solapamiento',
        'solape_legible': 'Solapamiento',
        'retraso_minutos': 'Retraso',
        'retraso_legible': 'Retraso',
        'diferencia_horas': 'Diferencia horaria',
        'umbral_horas': 'Umbral (horas)',
        'mismo_lugar': 'Misma ubicación',
        'cambio_terminal': 'Cambio de terminal',
        'zona_origen': 'Zona horaria de origen',
        'zona_destino': 'Zona horaria de destino',
        'margen_dias': 'Días de margen',
        'fecha_caducidad': 'Fecha de caducidad',
        'noches': 'Noches afectadas',
        'campos_faltantes': 'Campos que faltan',
    }

    for key, value in evidencia.items():
        if key in ('segmento_llegada', 'segmento_salida', 'elemento_a',
                   'elemento_b', 'reserva', 'llegada'):
            continue
        if key.endswith('_legible') and key.replace('_legible', '_minutos') in evidencia:
            continue
        if isinstance(value, bool):
            value = 'Sí' if value else 'No'
        elif isinstance(value, list):
            value = ', '.join(
                v.get('etiqueta', str(v)) if isinstance(v, dict) else str(v)
                for v in value
            )
        elif isinstance(value, dict):
            continue
        rows.append((labels.get(key, key.replace('_', ' ').capitalize()), value))

    entidades = [
        (key, evidencia[key])
        for key in ('segmento_llegada', 'segmento_salida', 'elemento_a',
                    'elemento_b', 'reserva', 'llegada')
        if key in evidencia
    ]

    return {'filas': rows, 'entidades': entidades, 'bruto': evidencia}
