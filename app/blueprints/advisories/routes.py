"""Security advisory routes (Jinja surface).

Specification section 2.7: advisories carry their source, date and confidence,
always show the disclaimer, and in phase 1 a manager validates one before
travellers can see it.
"""
import logging

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.advisories import advisories_bp
from app.services import advisory_service
from app.services.authorization_service import Permiso, can
from app.utils.decorators import require_advisory_access, require_trip_access
from app.utils.errors import AppError

logger = logging.getLogger(__name__)


@advisories_bp.route('/viaje/<trip_id>')
@login_required
@require_trip_access(Permiso.VER_RECOMENDACION)
def index(trip_id, trip):
    """List the advisories of a trip.

    A traveller sees only validated ones; a manager also sees drafts awaiting
    validation.
    """
    actor = current_user._get_current_object()
    advisories = advisory_service.list_for_trip(actor, trip)
    return render_template(
        'advisories/index.html',
        trip=trip,
        advisories=advisories,
        puede_validar=bool(can(actor, Permiso.VALIDAR_RECOMENDACION, trip)),
    )


@advisories_bp.route('/<advisory_id>')
@login_required
@require_advisory_access(Permiso.VER_RECOMENDACION)
def detail(advisory_id, advisory):
    """Advisory detail with sources and the disclaimer."""
    from app.models.advisory import DISCLAIMER

    return render_template(
        'advisories/detail.html',
        advisory=advisory,
        trip=advisory.trip,
        disclaimer=DISCLAIMER,
    )


@advisories_bp.route('/viaje/<trip_id>/generar', methods=['POST'])
@login_required
@require_trip_access(Permiso.GENERAR_RECOMENDACION)
def generate(trip_id, trip):
    """Generate advisories for the trip's destinations and dates."""
    try:
        created = advisory_service.generate_for_trip(
            current_user._get_current_object(), trip
        )
        flash(
            f'{len(created)} recomendaciones generadas. Revíselas y valídelas '
            'antes de que sean visibles para los viajeros.',
            'success',
        )
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('advisories.index', trip_id=trip.id))


@advisories_bp.route('/<advisory_id>/validar', methods=['POST'])
@login_required
@require_advisory_access(Permiso.VALIDAR_RECOMENDACION)
def validate(advisory_id, advisory):
    """Validate or reject an advisory (section 2.7)."""
    decision = request.form.get('decision', 'validar')
    try:
        advisory_service.validate(
            current_user._get_current_object(),
            advisory,
            aprobar=(decision == 'validar'),
            comentario=request.form.get('comentario'),
        )
        flash(
            'Recomendación validada y visible para los viajeros.'
            if decision == 'validar' else 'Recomendación rechazada.',
            'success' if decision == 'validar' else 'info',
        )
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('advisories.detail', advisory_id=advisory.id))
