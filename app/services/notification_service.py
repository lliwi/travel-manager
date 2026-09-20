"""Telling people what the system already knows.

A notification is created for a *fact*, not for an event: «this trip has an
open critical alert», not «the alert engine ran». The difference matters
because the engine reconciles and the source watcher runs nightly, so the same
fact presents itself again and again; keyed by the fact, the second telling
finds the first already there and says nothing.

Who gets told is decided the same way everything else is: by
``authorization_service``. Somebody who cannot see a trip is not told about it.
"""
import logging

from app.extensions import db
from app.models.enums import NotificationKind
from app.models.notification import Notification
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)


def notificar(usuario, tipo, clave, titulo, mensaje=None, enlace=None,
              trip=None, datos=None, commit=True):
    """Tell one person one thing, once.

    Returns the notification, whether it was created now or already existed.
    Re-telling does not resurrect something already read: a person who has read
    it has dealt with it, and putting it back unread would make the list a
    thing that grows back however often it is cleared.
    """
    if usuario is None:
        return None

    existente = Notification.query.filter_by(
        usuario_id=usuario.id, clave=clave,
    ).first()
    if existente is not None:
        return existente

    notificacion = Notification(
        usuario_id=usuario.id,
        tipo=NotificationKind.coerce(tipo, NotificationKind.VIAJE),
        clave=clave,
        titulo=titulo[:300],
        mensaje=mensaje,
        enlace=enlace,
        trip_id=getattr(trip, 'id', None),
        datos=datos or {},
    )
    db.session.add(notificacion)
    if commit:
        db.session.commit()
    return notificacion


def notificar_a_varios(usuarios, **kwargs):
    """Tell several people the same thing, each with their own copy."""
    creadas = []
    for usuario in usuarios:
        notificacion = notificar(usuario, commit=False, **kwargs)
        if notificacion is not None:
            creadas.append(notificacion)
    db.session.commit()
    return creadas


def interesados_en(trip):
    """Who should hear about this trip.

    Its manager and the people travelling on it. Not every administrator: an
    inbox that fills with other people's trips is an inbox nobody reads, and
    the whole point is that the person who has to act finds out.
    """
    gente = []
    if trip.gestor is not None and not trip.gestor.is_deleted:
        gente.append(trip.gestor)
    for viajero in trip.active_travelers:
        if viajero.user is not None and not viajero.user.is_deleted:
            gente.append(viajero.user)

    vistos = set()
    unicos = []
    for persona in gente:
        if persona.id not in vistos:
            vistos.add(persona.id)
            unicos.append(persona)
    return unicos


def sin_leer(usuario, limite=None):
    """This person's unread notifications, newest first."""
    query = (
        Notification.query
        .filter_by(usuario_id=usuario.id, leida_en=None)
        .order_by(Notification.created_at.desc())
    )
    return query.limit(limite).all() if limite else query.all()


def contar_sin_leer(usuario):
    return Notification.query.filter_by(
        usuario_id=usuario.id, leida_en=None,
    ).count()


def listar(usuario, incluir_leidas=True, limite=100):
    query = Notification.query.filter_by(usuario_id=usuario.id)
    if not incluir_leidas:
        query = query.filter(Notification.leida_en.is_(None))
    return query.order_by(Notification.created_at.desc()).limit(limite).all()


def marcar_leida(usuario, notification_id, commit=True):
    """Mark one as read, if it belongs to this person."""
    notificacion = Notification.query.filter_by(
        id=notification_id, usuario_id=usuario.id,
    ).first()
    if notificacion is None or notificacion.leida_en is not None:
        return notificacion

    notificacion.leida_en = utcnow()
    if commit:
        db.session.commit()
    return notificacion


def marcar_todas_leidas(usuario, commit=True):
    """Clear the list in one go."""
    pendientes = sin_leer(usuario)
    ahora = utcnow()
    for notificacion in pendientes:
        notificacion.leida_en = ahora
    if commit and pendientes:
        db.session.commit()
    return len(pendientes)


def enviar_pendientes_por_correo(limite=50):
    """Mail the notifications that have not been mailed yet.

    Separate from creating them on purpose: what lands in the application lands
    whether or not mail works, so the record of «we told them» does not depend
    on a server being reachable. This is the second delivery, and it is allowed
    to fail without taking the first with it.
    """
    from app.services import mail_service

    if not mail_service.esta_configurado():
        return 0

    pendientes = (
        Notification.query
        .filter(Notification.enviada_por_correo_en.is_(None))
        .filter(Notification.leida_en.is_(None))
        .order_by(Notification.created_at.asc())
        .limit(limite)
        .all()
    )

    enviadas = 0
    for notificacion in pendientes:
        usuario = notificacion.usuario
        if usuario is None or not usuario.email:
            notificacion.enviada_por_correo_en = utcnow()
            continue

        cuerpo = (notificacion.mensaje or '') + '\n'
        if notificacion.enlace:
            cuerpo += f'\n{notificacion.enlace}\n'

        try:
            if mail_service.enviar([usuario.email], notificacion.titulo, cuerpo):
                enviadas += 1
        except Exception as exc:
            logger.info('No se pudo enviar la notificación por correo: %s', exc)
            continue

        # Marked either way: a notification already read in the application does
        # not need chasing by mail, and one that failed to send should not be
        # retried for ever.
        notificacion.enviada_por_correo_en = utcnow()

    db.session.commit()
    return enviadas
