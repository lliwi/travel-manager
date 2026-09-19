"""User administration endpoints (specification section 7)."""
import uuid

from flask import current_app, request
from flask_login import current_user, login_required

from app.blueprints.api.v1 import api_v1_bp, ok, paginated
from app.extensions import db
from app.models.enums import RoleCode, UserStatus
from app.models.user import Role, User
from app.services import user_service
from app.utils.decorators import require_admin
from app.utils.errors import ResourceNotFound, ValidationError


@api_v1_bp.route('/users', methods=['GET'])
@login_required
@require_admin
def list_users():
    """List accounts."""
    query = User.query.filter(User.is_deleted.is_(False))

    if request.args.get('buscar'):
        needle = f'%{request.args["buscar"].lower()}%'
        query = query.filter(
            db.or_(
                db.func.lower(User.username).like(needle),
                db.func.lower(User.email).like(needle),
                db.func.lower(User.nombre).like(needle),
            )
        )
    if request.args.get('estado'):
        query = query.filter(User.estado == request.args['estado'])

    per_page = min(
        request.args.get('per_page', current_app.config['ITEMS_PER_PAGE'], type=int),
        current_app.config['API_MAX_PAGE_SIZE'],
    )
    pagination = query.order_by(User.nombre, User.apellidos).paginate(
        page=request.args.get('page', 1, type=int), per_page=per_page, error_out=False
    )
    return paginated(pagination, lambda u: u.to_dict())


@api_v1_bp.route('/users', methods=['POST'])
@login_required
@require_admin
def create_user():
    """Create a local account."""
    payload = request.get_json(silent=True) or {}
    for field in ('username', 'email', 'nombre', 'password'):
        if not payload.get(field):
            raise ValidationError(f'El campo «{field}» es obligatorio.')

    user = user_service.create_user(
        actor=current_user._get_current_object(),
        username=payload['username'],
        email=payload['email'],
        nombre=payload['nombre'],
        apellidos=payload.get('apellidos'),
        password=payload['password'],
        role_codes=payload.get('roles') or [RoleCode.USUARIO.value],
        puesto=payload.get('puesto'),
        departamento=payload.get('departamento'),
        must_change_password=bool(payload.get('must_change_password', True)),
    )
    return ok(user.to_dict(), status=201)


@api_v1_bp.route('/users/<user_id>', methods=['GET'])
@login_required
@require_admin
def get_user(user_id):
    """One account."""
    return ok(_load_user(user_id).to_dict())


@api_v1_bp.route('/users/<user_id>', methods=['PATCH'])
@login_required
@require_admin
def update_user(user_id):
    """Update an account."""
    payload = request.get_json(silent=True) or {}
    user = _load_user(user_id)

    campos = {
        field: payload[field]
        for field in ('email', 'nombre', 'apellidos', 'telefono', 'puesto',
                      'departamento', 'idioma', 'zona_horaria')
        if field in payload
    }
    if 'estado' in payload:
        campos['estado'] = UserStatus.coerce(payload['estado'], user.estado)

    user_service.update_user(
        actor=current_user._get_current_object(),
        user=user,
        role_codes=payload.get('roles'),
        password=payload.get('password'),
        **campos,
    )
    return ok(user.to_dict())


@api_v1_bp.route('/users/<user_id>', methods=['DELETE'])
@login_required
@require_admin
def delete_user(user_id):
    """Soft-delete an account."""
    user = _load_user(user_id)
    user_service.delete_user(current_user._get_current_object(), user)
    return ok({'mensaje': 'Usuario desactivado.'})


@api_v1_bp.route('/users/<user_id>/unlock', methods=['POST'])
@login_required
@require_admin
def unlock_user(user_id):
    """Clear a lockout after repeated failed logins."""
    user = _load_user(user_id)
    user_service.unlock_user(current_user._get_current_object(), user)
    return ok(user.to_dict())


@api_v1_bp.route('/roles', methods=['GET'])
@login_required
@require_admin
def list_roles():
    """The available roles."""
    return ok([
        {
            'id': str(r.id),
            'codigo': str(r.codigo),
            'nombre': r.nombre,
            'descripcion': r.descripcion,
        }
        for r in Role.query.order_by(Role.nombre).all()
    ])


def _load_user(user_id):
    try:
        user = db.session.get(User, uuid.UUID(str(user_id)))
    except (ValueError, TypeError):
        user = None
    if user is None or user.is_deleted:
        raise ResourceNotFound('El usuario indicado no existe.')
    return user
