"""Authentication endpoints.

The API shares the Flask-Login session cookie with the interface rather than
issuing a separate token. CSRF is satisfied with the ``X-CSRFToken`` header,
which Flask-WTF accepts -- exempting the API from CSRF would leave every
cookie-authenticated endpoint open to cross-site requests.

A bearer-token ``request_loader`` is the seam for phase 2 machine integrations.
"""
from flask import request, session
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf.csrf import generate_csrf

from app.blueprints.api.v1 import api_v1_bp, ok
from app.extensions import csrf, limiter
from app.models.enums import AuditResourceType
from app.services import audit_service
from app.services.identity import authenticate
from app.utils.decorators import authenticated_only, public_endpoint
from app.utils.errors import ValidationError


@api_v1_bp.route('/auth/login', methods=['POST'])
@csrf.exempt
@public_endpoint
@limiter.limit('10 per minute; 40 per hour')
def login():
    """Authenticate and start a session.

    Exempt from CSRF because this is the call that *issues* the token: there is
    no session to ride and nothing to protect yet. What remains -- an attacker
    logging a victim into an account the attacker controls -- is blocked by the
    ``SameSite=Lax`` session cookie and by requiring a JSON body, which a
    cross-site form cannot send. Every other endpoint keeps CSRF protection.
    """
    payload = request.get_json(silent=True) or {}
    username = payload.get('username') or payload.get('usuario')
    password = payload.get('password') or payload.get('contrasena')

    if not username or not password:
        raise ValidationError('Se requieren usuario y contraseña.')

    # Raises AuthenticationFailed / AccountLocked, which the blueprint's error
    # handler renders. The message is identical for every failure mode.
    user = authenticate(username, password)

    login_user(user, remember=bool(payload.get('remember_me')))
    session.permanent = bool(payload.get('remember_me'))

    return ok({
        'usuario': user.to_dict(),
        'csrf_token': generate_csrf(),
        'must_change_password': user.must_change_password,
    })


@api_v1_bp.route('/auth/logout', methods=['POST'])
@login_required
@authenticated_only
def logout():
    """End the session."""
    audit_service.record(
        'auth.logout',
        recurso_tipo=AuditResourceType.SESION,
        recurso_id=str(current_user.id),
    )
    logout_user()
    session.clear()
    return ok({'mensaje': 'Sesión finalizada.'})


@api_v1_bp.route('/me', methods=['GET'])
@login_required
@authenticated_only
def me():
    """The authenticated user, their roles and their effective permissions."""
    from app.services.authorization_service import PERMISOS_GLOBALES, Permiso, can

    actor = current_user._get_current_object()
    decision = can(actor, Permiso.VER_VIAJE, None)

    return ok({
        'usuario': actor.to_dict(),
        'permisos_globales': sorted(
            str(p) for p in decision.permisos & PERMISOS_GLOBALES
        ),
        'permisos': sorted(str(p) for p in decision.permisos),
        'csrf_token': generate_csrf(),
    })
