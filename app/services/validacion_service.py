"""Having a trip approved before it is bought.

The manager prepares the trip -- dates, travellers, itinerary, cost -- names
a validator and asks. The validator sees the whole trip and answers yes or no;
with ``VALIDACION_OBLIGATORIA`` on, only a yes lets the trip be confirmed.

Three rules carry the weight:

- Nobody validates a trip they have a stake in: not a traveller on it, not
  whoever asked.
- A request is for a trip that can be judged. One with no dates or nobody
  travelling would be approved on trust, which is what this exists to stop.
- Only the latest request lives on the trip. Asking again -- another
  validator, or the same after a rejection -- replaces it and withdraws the
  previous validator's view of the trip; the audit trail keeps every one.
"""
import logging

from app.extensions import db
from app.models.enums import AuditResourceType, RoleCode, ValidationState
from app.models.user import Role, User
from app.services import audit_service, notification_service, settings_service
from app.utils.errors import AuthorizationError, ConflictError, ValidationError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: A trip is put to validation while it is still a proposal. Once confirmed
#: it has been bought, and there is nothing left to approve.
ESTADOS_VALIDABLES = ('borrador', 'en_preparacion')


def obligatoria():
    """Whether a trip must be approved before it can be confirmed."""
    return settings_service.get_bool('VALIDACION_OBLIGATORIA', False)


def validadores_disponibles():
    """Active accounts holding the validator role, by name."""
    candidatos = (
        User.query.filter(User.is_deleted.is_(False))
        .join(User.roles).filter(Role.codigo == RoleCode.VALIDADOR)
        .order_by(User.nombre, User.apellidos).all()
    )
    return [u for u in candidatos if getattr(u, 'is_active_account', False)]


def validadores_para(trip, actor=None):
    """Who may be asked about this trip: validators with no stake in it."""
    return [
        u for u in validadores_disponibles()
        if not trip.has_traveler(u.id)
        and (actor is None or str(u.id) != str(actor.id))
    ]


def lo_que_falta(trip):
    """What the trip still lacks before anybody can be asked to judge it."""
    from app.services import trip_service

    faltan = []
    if not trip.inicio_utc or not trip.fin_utc:
        faltan.append('indicar las fechas de inicio y fin')
    if not trip.active_travelers:
        faltan.append('asignar al menos una persona viajera')
    if not trip_service._tiene_itinerario(trip):
        faltan.append('registrar algún trayecto, alojamiento o servicio')
    if settings_service.get_bool('COSTES_HABILITADOS', False) \
            and trip.coste_total is None and not trip.coste_estimado:
        faltan.append('anotar algún importe o el coste estimado')
    return faltan


def solicitar(actor, trip, validador, commit=True):
    """Put the trip before ``validador``."""
    if trip.pendiente_envio:
        raise ConflictError('El viaje todavía no se ha enviado a su gestor.')
    if not trip.is_editable or str(trip.estado) not in ESTADOS_VALIDABLES:
        raise ConflictError(
            f'Un viaje «{trip.estado.label}» ya no se envía a validar.'
        )

    faltan = lo_que_falta(trip)
    if faltan:
        raise ValidationError(
            f'Antes de pedir la validación falta {", y ".join(faltan)}.'
        )

    if validador is None or validador.is_deleted \
            or not getattr(validador, 'is_active_account', False) \
            or not validador.has_role(RoleCode.VALIDADOR):
        raise ValidationError('Elija un validador activo.')
    if str(validador.id) == str(actor.id):
        raise ValidationError('No puede validar un viaje quien pide su validación.')
    if trip.has_traveler(validador.id):
        raise ValidationError(
            f'{validador.nombre_completo} viaja en este viaje y no puede validarlo.'
        )

    trip.validador_id = validador.id
    trip.validacion_estado = ValidationState.PENDIENTE
    trip.validacion_solicitada_en = utcnow()
    trip.validacion_solicitada_por_id = actor.id
    trip.validacion_resuelta_en = None
    trip.validacion_comentario = None
    trip.validacion_version = None

    notification_service.notificar(
        validador,
        tipo='viaje',
        # Keyed by the request, not the trip: asking again after a rejection
        # is a new thing to be told about.
        clave=f'validacion_solicitada:{trip.id}:{trip.validacion_solicitada_en.isoformat()}',
        titulo=f'{actor.nombre_completo} le pide validar «{trip.titulo}»',
        mensaje=(
            f'El viaje {trip.referencia} está preparado y espera su validación '
            'antes de gestionar la compra.'
        ),
        enlace=_enlace(trip),
        trip=trip,
        commit=False,
    )

    if commit:
        db.session.commit()
        audit_service.record(
            'trip.validation_requested',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'referencia': trip.referencia,
                       'validador': validador.username},
        )
    return trip


def resolver(actor, trip, aprobar, comentario=None, commit=True):
    """Record the validator's answer."""
    from app.services import authorization_service

    if not authorization_service.es_validador_de(actor, trip):
        raise AuthorizationError('Este viaje no se le ha pedido validar.')
    if trip.validacion_estado is not ValidationState.PENDIENTE:
        raise ConflictError('Esta validación ya se respondió.')
    if not trip.is_editable:
        raise ConflictError('Un viaje cancelado no se valida.')
    # Checked again here and not only when asked: the manager may have added
    # them as a traveller in between.
    if trip.has_traveler(actor.id):
        raise AuthorizationError('No puede validar un viaje en el que viaja.')

    comentario = (comentario or '').strip() or None
    if not aprobar and not comentario:
        raise ValidationError('Indique por qué rechaza el viaje.')

    trip.validacion_estado = (
        ValidationState.APROBADA if aprobar else ValidationState.RECHAZADA
    )
    trip.validacion_resuelta_en = utcnow()
    trip.validacion_comentario = comentario
    trip.validacion_version = trip.itinerary_version or 0

    destinatarios = {
        u.id: u for u in (trip.gestor, db.session.get(User, trip.validacion_solicitada_por_id)
                          if trip.validacion_solicitada_por_id else None)
        if u is not None and not u.is_deleted
    }
    for destinatario in destinatarios.values():
        notification_service.notificar(
            destinatario,
            tipo='viaje',
            clave=f'validacion_resuelta:{trip.id}:{trip.validacion_resuelta_en.isoformat()}',
            titulo=(
                f'{actor.nombre_completo} ha '
                f'{"validado" if aprobar else "rechazado"} «{trip.titulo}»'
            ),
            mensaje=(
                (f'El viaje {trip.referencia} ya puede confirmarse.' if aprobar
                 else f'El viaje {trip.referencia} no se ha aprobado.')
                + (f' Comentario: {comentario}' if comentario else '')
            ),
            enlace=_enlace(trip),
            trip=trip,
            commit=False,
        )

    if commit:
        db.session.commit()
        audit_service.record(
            'trip.validation_approved' if aprobar else 'trip.validation_rejected',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'referencia': trip.referencia, 'comentario': comentario},
        )
    return trip


def _enlace(trip):
    from flask import url_for

    try:
        return url_for('trips.detail', trip_id=trip.id)
    except RuntimeError:
        return None
