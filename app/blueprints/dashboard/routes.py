"""Dashboard: what this actor should look at next.

The content differs by role because the roles want different things: a manager
wants open alerts and documents awaiting review, a traveller wants their next
trip.
"""
from flask import flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.blueprints.dashboard import dashboard_bp
from app.extensions import db
from app.models.alert import Alert
from app.models.document import Document
from app.models.enums import (
    ALERT_OPEN_STATES,
    AlertSeverity,
    DocumentProcessState,
    TripStatus,
)
from app.models.trip import Trip
from app.services.authorization_service import Permiso, visible_trips_query
from app.utils.decorators import authenticated_only, require_permiso
from app.utils.timeutil import utcnow


@dashboard_bp.route('/')
@login_required
def index():
    """Landing page after login."""
    actor = current_user._get_current_object()
    visible = visible_trips_query(actor)
    now = utcnow()

    proximos = (
        visible.filter(
            Trip.inicio_utc.isnot(None),
            Trip.inicio_utc >= now,
            Trip.estado.notin_([TripStatus.CANCELADO.value, TripStatus.FINALIZADO.value]),
        )
        .order_by(Trip.inicio_utc.asc())
        .limit(5)
        .all()
    )

    en_curso = (
        visible.filter(Trip.estado == TripStatus.EN_CURSO.value)
        .order_by(Trip.inicio_utc.asc())
        .limit(5)
        .all()
    )

    # Subquery of trip ids this actor may see, reused by the alert and document
    # counts so neither can leak across the authorisation boundary.
    trip_ids = visible.with_entities(Trip.id).subquery()

    alertas = (
        Alert.query.filter(
            Alert.trip_id.in_(db.select(trip_ids.c.id)),
            Alert.estado.in_([str(s) for s in ALERT_OPEN_STATES]),
        )
        .order_by(Alert.generada_en.desc())
        .limit(8)
        .all()
    )

    resumen = {
        'viajes_totales': visible.count(),
        'viajes_en_curso': visible.filter(
            Trip.estado == TripStatus.EN_CURSO.value
        ).count(),
        'alertas_abiertas': Alert.query.filter(
            Alert.trip_id.in_(db.select(trip_ids.c.id)),
            Alert.estado.in_([str(s) for s in ALERT_OPEN_STATES]),
        ).count(),
        'alertas_criticas': Alert.query.filter(
            Alert.trip_id.in_(db.select(trip_ids.c.id)),
            Alert.estado.in_([str(s) for s in ALERT_OPEN_STATES]),
            Alert.severidad.in_([AlertSeverity.ALTA.value, AlertSeverity.CRITICA.value]),
        ).count(),
    }

    pendientes_revision = []
    if actor.is_gestor or actor.is_administrador:
        pendientes_revision = (
            Document.query.filter(
                Document.trip_id.in_(db.select(trip_ids.c.id)),
                Document.is_deleted.is_(False),
                Document.estado_proceso == DocumentProcessState.PENDIENTE_REVISION.value,
            )
            .order_by(Document.updated_at.desc())
            .limit(8)
            .all()
        )
        resumen['documentos_pendientes'] = len(pendientes_revision)

    return render_template(
        'dashboard/index.html',
        resumen=resumen,
        proximos=proximos,
        en_curso=en_curso,
        alertas=alertas,
        pendientes_revision=pendientes_revision,
    )


@dashboard_bp.route('/notificaciones')
@login_required
@authenticated_only
def notifications():
    """This person's notifications.

    Authenticated and nothing more: a notification was created for one person
    and is only ever read by that person, so there is no resource here to
    authorise against -- the scoping is the query.
    """
    from app.services import notification_service

    actor = current_user._get_current_object()
    return render_template(
        'dashboard/notifications.html',
        notificaciones=notification_service.listar(actor),
        sin_leer=notification_service.contar_sin_leer(actor),
    )


@dashboard_bp.route('/notificaciones/<notification_id>/leida', methods=['POST'])
@login_required
@authenticated_only
def read_notification(notification_id):
    """Mark one as read and go where it points."""
    from app.services import notification_service

    actor = current_user._get_current_object()
    notificacion = notification_service.marcar_leida(actor, notification_id)

    destino = (notificacion.enlace if notificacion else None) or url_for(
        'dashboard.notifications'
    )
    return redirect(destino)


@dashboard_bp.route('/notificaciones/leidas', methods=['POST'])
@login_required
@authenticated_only
def read_all_notifications():
    """Clear the list in one go."""
    from app.services import notification_service

    actor = current_user._get_current_object()
    cuantas = notification_service.marcar_todas_leidas(actor)
    flash(f'{cuantas} notificaciones marcadas como leídas.', 'info')
    return redirect(url_for('dashboard.notifications'))


@dashboard_bp.route('/informes')
@login_required
@require_permiso(Permiso.VER_VIAJE)
def reports():
    """What is happening across the trips this person may see.

    Guarded by the ordinary permission rather than by role: the numbers are
    already scoped to what the asker may see, so a traveller reading this reads
    a report about their own trips, which is a fair thing for them to have.
    """
    from app.services import report_service

    actor = current_user._get_current_object()
    return render_template(
        'dashboard/reports.html', informe=report_service.informe_completo(actor),
    )
