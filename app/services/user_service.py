"""Account administration.

Specification section 2.1 requires every sensitive user operation to be audited:
creation, deletion and role changes all leave a trace here.
"""
import logging

from app.extensions import db
from app.models.enums import AuditResourceType, IdentityProviderCode, UserStatus
from app.models.user import Role, User
from app.services import audit_service
from app.services.identity.local import validate_password_strength
from app.utils.crypto import hash_password
from app.utils.errors import ConflictError, ValidationError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)


def normalizar_email(email):
    """Validate an address and return it in its stored form.

    The one place that decides what an address may look like. It used to be
    decided only by the administration form, which was stricter than everything
    else: ``flask seed`` and ``flask create-admin`` issue accounts on
    ``@corp.test``, and the form then refused to save them -- so a seeded
    account could not be edited at all, not even to change its password.

    Reserved and internal-only domains are accepted on purpose. An on-premise
    deployment addresses its people on names that never resolve publicly, and
    this application issues such addresses itself; refusing them would be
    refusing its own data. Everything malformed is still refused.
    """
    from email_validator import EmailNotValidError, validate_email

    if email is None:
        raise ValidationError('Indique el correo electrónico.')

    limpio = str(email).strip()
    if not limpio:
        raise ValidationError('Indique el correo electrónico.')

    try:
        # ``test_environment`` is what this library calls "do not reject
        # reserved domains"; the name is about .test, the reason here is an
        # internal deployment.
        resultado = validate_email(
            limpio, check_deliverability=False, test_environment=True,
        )
    except EmailNotValidError as exc:
        raise ValidationError('El correo electrónico no es válido.') from exc

    return resultado.normalized.lower()


def create_user(actor, username, email, nombre, password, apellidos=None,
                role_codes=None, puesto=None, departamento=None,
                estado=UserStatus.ACTIVO, must_change_password=False, commit=True):
    """Create a local account."""
    username = str(username).strip()
    email = normalizar_email(email)

    if _exists(username, email):
        raise ConflictError('Ya existe un usuario con ese nombre o correo electrónico.')

    problems = validate_password_strength(password)
    if problems:
        raise ValidationError(problems[0], detalles={'requisitos': problems})

    from app.services import settings_service

    user = User(
        username=username,
        email=email,
        # The organisation's zone, not the one hardcoded on the column. Every
        # account carries a timezone and that is what a trip uses, so a column
        # default of «Europe/Madrid» quietly overrode the setting for everyone.
        zona_horaria=settings_service.zona_horaria_por_defecto(),
        idioma=settings_service.idioma_por_defecto(),
        nombre=nombre,
        apellidos=apellidos,
        puesto=puesto,
        departamento=departamento,
        password_hash=hash_password(password),
        password_changed_at=utcnow(),
        must_change_password=must_change_password,
        estado=estado,
        identity_provider=IdentityProviderCode.LOCAL,
    )
    db.session.add(user)
    db.session.flush()

    _apply_roles(user, role_codes)

    if commit:
        db.session.commit()
        audit_service.record(
            'user.created',
            recurso_tipo=AuditResourceType.USUARIO,
            recurso_id=str(user.id),
            actor=actor,
            metadatos={'username': user.username, 'roles': user.role_codes},
        )
    return user


def update_user(actor, user, role_codes=None, password=None, commit=True, **campos):
    """Update an account, auditing what changed."""
    cambios = []

    for field in ('email', 'nombre', 'apellidos', 'puesto', 'departamento',
                  'telefono', 'estado', 'idioma', 'zona_horaria'):
        if field not in campos:
            continue
        nuevo = campos[field]
        if field == 'email' and nuevo:
            nuevo = normalizar_email(nuevo)
            if _exists(None, nuevo, exclude_id=user.id):
                raise ConflictError('Ya existe otro usuario con ese correo electrónico.')
        if getattr(user, field) != nuevo:
            setattr(user, field, nuevo)
            cambios.append(field)

    # Deactivating an account must also end its live sessions.
    if 'estado' in campos and user.estado is not UserStatus.ACTIVO:
        user.bump_session_epoch()

    if role_codes is not None:
        anteriores = set(user.role_codes)
        _apply_roles(user, role_codes, replace=True)
        if anteriores != set(user.role_codes):
            cambios.append('roles')
            audit_service.record(
                'user.roles_changed',
                recurso_tipo=AuditResourceType.ROL,
                recurso_id=str(user.id),
                actor=actor,
                metadatos={
                    'antes': sorted(anteriores),
                    'despues': sorted(user.role_codes),
                },
                commit=False,
            )

    if password and not user.es_local:
        # The login path routes by `identity_provider`, so this hash would
        # never be consulted: a password that can be set and never works is
        # worse than one that cannot be set, because somebody will rely on it
        # the day the directory is down.
        raise ValidationError(
            'Esta cuenta se autentica contra el directorio corporativo; su '
            'contraseña se cambia allí, no aquí.'
        )

    if password:
        problems = validate_password_strength(password)
        if problems:
            raise ValidationError(problems[0], detalles={'requisitos': problems})
        user.password_hash = hash_password(password)
        user.password_changed_at = utcnow()
        user.bump_session_epoch()
        cambios.append('password')

    if commit:
        db.session.commit()
        if cambios:
            audit_service.record(
                'user.updated',
                recurso_tipo=AuditResourceType.USUARIO,
                recurso_id=str(user.id),
                actor=actor,
                metadatos={'campos': sorted(set(cambios))},
            )
    return user


def delete_user(actor, user, commit=True):
    """Soft-delete an account.

    Never a hard delete: the audit trail, trip history and provenance rows all
    reference the user, and section 3.2 requires that history to stay intact.
    Erasure under a retention or RGPD request is a separate, explicit operation.
    """
    if str(user.id) == str(getattr(actor, 'id', None)):
        raise ValidationError('No puede eliminar su propia cuenta.')

    if user.is_administrador and _count_active_admins() <= 1:
        raise ConflictError(
            'No puede eliminar al único administrador activo del sistema.'
        )

    user.soft_delete(actor)
    user.estado = UserStatus.INACTIVO
    user.bump_session_epoch()

    if commit:
        db.session.commit()
        audit_service.record(
            'user.deleted',
            recurso_tipo=AuditResourceType.USUARIO,
            recurso_id=str(user.id),
            actor=actor,
            metadatos={'username': user.username},
        )
    return user


def unlock_user(actor, user, commit=True):
    """Clear a lockout after repeated failed logins."""
    user.failed_login_count = 0
    user.locked_until = None
    if commit:
        db.session.commit()
        audit_service.record(
            'user.unlocked',
            recurso_tipo=AuditResourceType.USUARIO,
            recurso_id=str(user.id),
            actor=actor,
        )
    return user


def _apply_roles(user, role_codes, replace=False):
    """Set the account's roles from a list of role codes."""
    if role_codes is None:
        return
    roles = [r for r in (Role.get(code) for code in role_codes) if r is not None]
    if replace:
        user.roles = roles
        user.bump_session_epoch()
    else:
        for role in roles:
            if role not in user.roles:
                user.roles.append(role)


def _exists(username=None, email=None, exclude_id=None):
    """True when another account already uses this username or email."""
    clauses = []
    if username:
        clauses.append(db.func.lower(User.username) == username.lower())
    if email:
        clauses.append(db.func.lower(User.email) == email.lower())
    if not clauses:
        return False

    query = User.query.filter(db.or_(*clauses))
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return query.first() is not None


def _count_active_admins():
    """How many active administrators remain."""
    from app.models.enums import RoleCode

    return sum(
        1 for user in User.query.filter(
            User.is_deleted.is_(False),
            User.estado == UserStatus.ACTIVO.value,
        ).all()
        if user.has_role(RoleCode.ADMINISTRADOR)
    )
