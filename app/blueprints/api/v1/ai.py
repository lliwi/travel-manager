"""AI assistant, research and security advisory endpoints.

Specification sections 2.5, 2.6, 2.7 and acceptance flow 5.4. The context handed
to the model is built by ``authorization_service.build_ai_context`` from what the
caller may already see, and the references in the answer are validated back
against that same set.
"""
from flask import request
from flask_login import current_user, login_required

from app.blueprints.api.v1 import api_v1_bp, ok
from app.services import advisory_service, ai_service
from app.services.authorization_service import Permiso
from app.utils.decorators import require_advisory_access, require_trip_access
from app.utils.errors import ValidationError


@api_v1_bp.route('/trips/<trip_id>/ai/query', methods=['POST'])
@login_required
@require_trip_access(Permiso.CONSULTAR_IA)
def ai_query(trip_id, trip):
    """Ask a question about a trip (specification flow 5.4)."""
    payload = request.get_json(silent=True) or {}
    pregunta = payload.get('pregunta') or payload.get('question')
    if not pregunta or not str(pregunta).strip():
        raise ValidationError('Se requiere una pregunta.')

    result = ai_service.answer_trip_question(
        actor=current_user._get_current_object(),
        trip=trip,
        pregunta=str(pregunta).strip(),
    )
    return ok(result)


@api_v1_bp.route('/trips/<trip_id>/ai/summary', methods=['POST'])
@login_required
@require_trip_access(Permiso.CONSULTAR_IA)
def ai_summary(trip_id, trip):
    """Generate an executive summary and a readable itinerary."""
    result = ai_service.summarize_trip(
        actor=current_user._get_current_object(), trip=trip
    )
    return ok(result)


@api_v1_bp.route('/trips/<trip_id>/research', methods=['POST'])
@login_required
@require_trip_access(Permiso.INVESTIGAR_WEB)
def research(trip_id, trip):
    """Consult the allow-listed public sources (section 2.6).

    Assisted search only: it never books or purchases anything, and the manager
    keeps the final decision.
    """
    payload = request.get_json(silent=True) or {}
    consulta = payload.get('consulta') or payload.get('query')
    if not consulta or not str(consulta).strip():
        raise ValidationError('Se requiere una consulta.')

    result = ai_service.research_public_info(
        actor=current_user._get_current_object(),
        trip=trip,
        consulta=str(consulta).strip(),
        categoria=payload.get('categoria'),
    )
    return ok(result)


@api_v1_bp.route('/trips/<trip_id>/security-advisories', methods=['GET'])
@login_required
@require_trip_access(Permiso.VER_RECOMENDACION)
def list_advisories(trip_id, trip):
    """The trip's advisories. Travellers see only validated ones."""
    advisories = advisory_service.list_for_trip(
        current_user._get_current_object(), trip
    )
    return ok([a.to_dict() for a in advisories])


@api_v1_bp.route('/trips/<trip_id>/security-advisories', methods=['POST'])
@login_required
@require_trip_access(Permiso.GENERAR_RECOMENDACION)
def generate_advisories(trip_id, trip):
    """Generate advisories for the trip's destinations and dates."""
    created = advisory_service.generate_for_trip(
        current_user._get_current_object(), trip
    )
    return ok([a.to_dict() for a in created], status=201)


@api_v1_bp.route('/security-advisories/<advisory_id>', methods=['PATCH'])
@login_required
@require_advisory_access(Permiso.VALIDAR_RECOMENDACION)
def validate_advisory(advisory_id, advisory):
    """Validate or reject an advisory (section 2.7)."""
    payload = request.get_json(silent=True) or {}
    advisory_service.validate(
        current_user._get_current_object(),
        advisory,
        aprobar=bool(payload.get('aprobar', True)),
        comentario=payload.get('comentario'),
    )
    return ok(advisory.to_dict())


@api_v1_bp.route('/alerts/<alert_id>/explain', methods=['POST'])
@login_required
def explain_alert(alert_id):
    """Explain an alert and propose corrective actions (section 2.5)."""
    from app.services.authorization_service import authorize_alert

    actor = current_user._get_current_object()
    alert = authorize_alert(actor, alert_id, Permiso.CONSULTAR_IA)
    return ok(ai_service.explain_alert(actor=actor, alert=alert))


explain_alert.__authz__ = (str(Permiso.CONSULTAR_IA), 'alert_id')
