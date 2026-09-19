"""Alert endpoints (specification section 7)."""
from flask import request
from flask_login import current_user, login_required

from app.blueprints.api.v1 import api_v1_bp, ok
from app.services import alert_service
from app.services.authorization_service import Permiso
from app.utils.decorators import require_alert_access, require_trip_access
from app.utils.errors import ValidationError


@api_v1_bp.route('/trips/<trip_id>/alerts', methods=['GET'])
@login_required
@require_trip_access(Permiso.VER_ALERTA)
def list_alerts(trip_id, trip):
    """A trip's alerts, scoped to what the caller may see."""
    alerts = alert_service.list_for_trip(
        current_user._get_current_object(),
        trip,
        estado=request.args.getlist('estado') or None,
        severidad=request.args.getlist('severidad') or None,
    )
    return ok([a.to_dict() for a in alerts])


@api_v1_bp.route('/trips/<trip_id>/alerts', methods=['PATCH'])
@login_required
@require_trip_access(Permiso.RECALCULAR_ALERTAS)
def recalculate_alerts(trip_id, trip):
    """Re-run the alert engine over this trip."""
    from app.models.enums import AlertTrigger

    run = alert_service.recalculate(
        trip, trigger=AlertTrigger.MANUAL, actor=current_user._get_current_object()
    )
    return ok(run.to_dict())


@api_v1_bp.route('/alerts/<alert_id>', methods=['GET'])
@login_required
@require_alert_access(Permiso.VER_ALERTA)
def get_alert(alert_id, alert):
    """One alert with its evidence and state history."""
    return ok(alert.to_dict(include_transitions=True))


@api_v1_bp.route('/alerts/<alert_id>', methods=['PATCH'])
@login_required
@require_alert_access(Permiso.GESTIONAR_ALERTA)
def update_alert(alert_id, alert):
    """Accept, resolve or dismiss an alert with a comment (section 5.3)."""
    payload = request.get_json(silent=True) or {}
    nuevo = payload.get('estado')
    if not nuevo:
        raise ValidationError('Se requiere el nuevo estado de la alerta.')

    alert_service.change_state(
        current_user._get_current_object(),
        alert,
        nuevo,
        comentario=payload.get('comentario'),
        responsable_id=payload.get('responsable_id'),
    )
    return ok(alert.to_dict(include_transitions=True))
