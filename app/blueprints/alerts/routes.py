"""Alert routes (Jinja surface)."""
import logging

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.alerts import alerts_bp
from app.models.enums import AlertSeverity, AlertState
from app.services import alert_service
from app.services.authorization_service import Permiso, can
from app.utils.decorators import (
    rate_limited,
    require_alert_access,
    require_trip_access,
)
from app.utils.errors import AppError

logger = logging.getLogger(__name__)


@alerts_bp.route('/viaje/<trip_id>')
@login_required
@require_trip_access(Permiso.VER_ALERTA)
def index(trip_id, trip):
    """List a trip's alerts, scoped to what this actor may see."""
    alerts = alert_service.list_for_trip(
        current_user._get_current_object(),
        trip,
        estado=request.args.get('estado') or None,
        severidad=request.args.get('severidad') or None,
    )
    return render_template(
        'alerts/index.html',
        trip=trip,
        alerts=alerts,
        AlertState=AlertState,
        AlertSeverity=AlertSeverity,
    )


@alerts_bp.route('/<alert_id>')
@login_required
@require_alert_access(Permiso.VER_ALERTA)
def detail(alert_id, alert):
    """Alert detail with the evidence that produced it (section 5.3)."""
    actor = current_user._get_current_object()
    return render_template(
        'alerts/detail.html',
        alert=alert,
        trip=alert.trip,
        evidencia=alert_service.render_evidence(alert),
        puede_consultar_ia=bool(can(actor, Permiso.CONSULTAR_IA, alert)),
    )


@alerts_bp.route('/<alert_id>/explicar', methods=['POST'])
@login_required
@require_alert_access(Permiso.CONSULTAR_IA)
@rate_limited('10 per minute; 60 per hour')
def explain(alert_id, alert):
    """Explain one alert in plain language, with corrective actions.

    A concrete question about a concrete alert: the model reads the evidence
    the engine already recorded and nothing else, so it can restate and advise
    but never discover a problem the rules did not find.
    """
    from app.services import ai_service

    explicacion = None
    try:
        explicacion = ai_service.explain_alert(
            current_user._get_current_object(), alert
        )
    except AppError as error:
        flash(error.mensaje, 'danger')
    except Exception:
        logger.exception('Fallo al explicar la alerta %s', alert.id)
        flash(
            'El asistente no está disponible en este momento. '
            'Inténtelo de nuevo en unos minutos.',
            'danger',
        )

    return render_template(
        'alerts/detail.html',
        alert=alert,
        trip=alert.trip,
        evidencia=alert_service.render_evidence(alert),
        explicacion=explicacion,
        puede_consultar_ia=True,
    )


@alerts_bp.route('/<alert_id>/estado', methods=['POST'])
@login_required
@require_alert_access(Permiso.GESTIONAR_ALERTA)
def change_state(alert_id, alert):
    """Accept, resolve or dismiss an alert, with a comment (section 5.3)."""
    nuevo = request.form.get('estado')
    comentario = request.form.get('comentario')
    try:
        alert_service.change_state(
            current_user._get_current_object(), alert, nuevo, comentario
        )
        flash('Alerta actualizada.', 'success')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('alerts.detail', alert_id=alert.id))


@alerts_bp.route('/viaje/<trip_id>/recalcular', methods=['POST'])
@login_required
@require_trip_access(Permiso.RECALCULAR_ALERTAS)
def recalculate(trip_id, trip):
    """Re-run the alert engine over this trip."""
    from app.models.enums import AlertTrigger

    run = alert_service.recalculate(
        trip, trigger=AlertTrigger.MANUAL, actor=current_user._get_current_object()
    )
    flash(
        f'Alertas recalculadas: {run.creadas} nuevas, {run.actualizadas} actualizadas, '
        f'{run.cerradas} cerradas.',
        'info',
    )
    return redirect(url_for('alerts.index', trip_id=trip.id))
