"""Audit endpoints (specification section 7, administration only)."""
from flask import current_app, request
from flask_login import login_required

from app.blueprints.api.v1 import api_v1_bp, ok, paginated
from app.models.audit import AuditEvent
from app.services import audit_service
from app.utils.decorators import require_admin


@api_v1_bp.route('/audit-events', methods=['GET'])
@login_required
@require_admin
def list_audit_events():
    """Browse the audit trail with filters and pagination."""
    query = AuditEvent.query

    if request.args.get('accion'):
        query = query.filter(AuditEvent.accion.like(f'%{request.args["accion"]}%'))
    if request.args.get('actor_id'):
        query = query.filter(AuditEvent.actor_id == request.args['actor_id'])
    if request.args.get('recurso_tipo'):
        query = query.filter(AuditEvent.recurso_tipo == request.args['recurso_tipo'])
    if request.args.get('recurso_id'):
        query = query.filter(AuditEvent.recurso_id == str(request.args['recurso_id']))
    if request.args.get('resultado'):
        query = query.filter(AuditEvent.resultado == request.args['resultado'])
    if request.args.get('desde'):
        query = query.filter(AuditEvent.created_at >= request.args['desde'])
    if request.args.get('hasta'):
        query = query.filter(AuditEvent.created_at <= request.args['hasta'])

    per_page = min(
        request.args.get('per_page', 50, type=int),
        current_app.config['API_MAX_PAGE_SIZE'],
    )
    pagination = query.order_by(AuditEvent.id.desc()).paginate(
        page=request.args.get('page', 1, type=int), per_page=per_page, error_out=False
    )
    return paginated(pagination, lambda e: e.to_dict())


@api_v1_bp.route('/audit-events/verify', methods=['POST'])
@login_required
@require_admin
def verify_audit_chain():
    """Verify the integrity of the audit hash chain."""
    ok_chain, problems = audit_service.verify_chain(
        limit=request.args.get('limit', type=int)
    )
    return ok({'integra': ok_chain, 'anomalias': problems})
