"""Identity provider registry and the login entry point.

Everything above this module talks to ``authenticate()`` and never to a concrete
provider, which is the decoupling specification section 3.3 requires. Adding the
LDAP provider in phase 2 means registering one class here.
"""
import logging

from flask import current_app

from app.extensions import db
from app.models.enums import AuditResourceType, AuditResult, IdentityProviderCode, RoleCode
from app.models.user import Role, User
from app.services import audit_service
from app.services.identity.base import IdentityProvider
from app.services.identity.local import LocalIdentityProvider
from app.utils.errors import AccountLocked, AuthenticationFailed
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Registered provider classes, keyed by their code.
_PROVIDERS = {
    LocalIdentityProvider.codigo: LocalIdentityProvider,
}


def register_provider(provider_cls):
    """Register an identity provider implementation.

    Phase 2 calls this with the LDAP/Active Directory provider.
    """
    if not issubclass(provider_cls, IdentityProvider):
        raise TypeError('El proveedor debe heredar de IdentityProvider.')
    _PROVIDERS[provider_cls.codigo] = provider_cls
    return provider_cls


def available_providers():
    """Codes of every registered provider."""
    return sorted(_PROVIDERS)


def get_provider(codigo=None):
    """Build the configured provider instance.

    Args:
        codigo: Provider code, defaulting to the ``IDENTITY_PROVIDER`` setting.
    """
    codigo = str(codigo or current_app.config['IDENTITY_PROVIDER'])
    provider_cls = _PROVIDERS.get(codigo)
    if provider_cls is None:
        logger.error(
            'Proveedor de identidad desconocido: %s. Se usa el local.', codigo
        )
        provider_cls = LocalIdentityProvider

    if provider_cls is LocalIdentityProvider:
        return LocalIdentityProvider(
            max_attempts=current_app.config['LOGIN_MAX_ATTEMPTS'],
            lockout_minutes=current_app.config['LOGIN_LOCKOUT_MINUTES'],
        )
    return provider_cls()


def authenticate(username, credential, provider_code=None):
    """Authenticate a credential and return the matching local ``User``.

    Every outcome is audited. The exception raised on failure is deliberately
    the same whatever went wrong, so the interface cannot be used to work out
    which usernames exist -- the specific reason goes to the audit trail instead.

    Raises:
        AuthenticationFailed: The credential is wrong, unknown or the account
            may not be used.
        AccountLocked: A lockout from repeated failures is in force.

    Returns:
        The authenticated :class:`User`.
    """
    provider = get_provider(provider_code)
    result = provider.authenticate(username, credential)

    if not result.exito:
        _audit_failure(username, provider, result)
        if result.bloqueada:
            raise AccountLocked()
        raise AuthenticationFailed()

    user = _resolve_local_user(provider, result.record)
    if user is None:
        _audit_failure(username, provider, result, motivo='sin_cuenta_local')
        raise AuthenticationFailed()

    if result.necesita_rehash and credential:
        _upgrade_hash(user, credential)

    audit_service.record(
        'auth.login',
        recurso_tipo=AuditResourceType.SESION,
        recurso_id=str(user.id),
        actor=user,
        metadatos={'proveedor': provider.codigo},
    )
    return user


def _resolve_local_user(provider, record):
    """Map a provider record onto the local ``User`` row.

    For the local provider this is a direct lookup. For a directory provider it
    is where just-in-time provisioning happens, creating the account on first
    successful login and refreshing its attributes afterwards.
    """
    if record is None:
        return None

    user = User.query.filter_by(
        identity_provider=provider.codigo,
        external_id=record.external_id,
    ).first()

    if user is None and provider.codigo == IdentityProviderCode.LOCAL.value:
        # Local accounts store the UUID as their own identity, so external_id is
        # the primary key rather than a separate column.
        import uuid

        try:
            user = db.session.get(User, uuid.UUID(record.external_id))
        except (ValueError, TypeError):
            user = None

    if user is None and provider.supports_provisioning():
        if not current_app.config['IDENTITY_JIT_PROVISIONING']:
            logger.warning(
                'Identidad %s autenticada pero el aprovisionamiento JIT está desactivado.',
                record.username,
            )
            return None
        user = provision_user(provider, record)

    elif user is not None and provider.supports_provisioning():
        _sync_attributes(provider, user, record)

    return user


def provision_user(provider, record):
    """Create a local account from a directory identity (JIT provisioning).

    Phase 2. The account is created with whatever roles the group mapping
    resolves to, defaulting to ``usuario`` -- never to an elevated role.
    """
    user = User(
        username=record.username,
        email=record.email or f'{record.username}@desconocido.local',
        nombre=record.nombre or record.username,
        apellidos=record.apellidos,
        telefono=record.telefono,
        puesto=record.puesto,
        departamento=record.departamento,
        identity_provider=provider.codigo,
        external_id=record.external_id,
        password_hash=None,
        external_attributes=dict(record.atributos or {}),
        synced_at=utcnow(),
    )
    db.session.add(user)
    db.session.flush()

    for code in provider.map_groups_to_roles(record.grupos) or [RoleCode.USUARIO.value]:
        role = Role.get(code)
        if role:
            user.roles.append(role)

    db.session.commit()
    audit_service.record(
        'user.provisioned',
        recurso_tipo=AuditResourceType.USUARIO,
        recurso_id=str(user.id),
        actor=None,
        metadatos={'proveedor': provider.codigo, 'username': record.username},
    )
    logger.info('Usuario aprovisionado desde %s: %s', provider.codigo, record.username)
    return user


def _sync_attributes(provider, user, record):
    """Refresh a directory-backed account's attributes on login."""
    changed = False
    for attr, value in (
        ('email', record.email),
        ('nombre', record.nombre),
        ('apellidos', record.apellidos),
        ('telefono', record.telefono),
        ('puesto', record.puesto),
        ('departamento', record.departamento),
    ):
        if value and getattr(user, attr) != value:
            setattr(user, attr, value)
            changed = True

    mapped = provider.map_groups_to_roles(record.grupos)
    if mapped and set(mapped) != set(user.role_codes):
        user.roles = [r for r in (Role.get(c) for c in mapped) if r]
        user.bump_session_epoch()
        changed = True

    if changed:
        user.synced_at = utcnow()
        db.session.commit()


def _upgrade_hash(user, credential):
    """Re-hash a password stored with outdated Argon2 parameters."""
    from app.utils.crypto import hash_password

    try:
        user.password_hash = hash_password(credential)
        user.password_changed_at = user.password_changed_at or utcnow()
        db.session.commit()
        logger.info('Hash de contraseña actualizado para %s', user.username)
    except Exception:
        db.session.rollback()
        logger.exception('No se pudo actualizar el hash de %s', user.username)


def _audit_failure(username, provider, result, motivo=None):
    """Record a failed login without ever writing the credential."""
    audit_service.record(
        'auth.login_failed',
        recurso_tipo=AuditResourceType.SESION,
        actor=None,
        resultado=AuditResult.DENEGADO,
        metadatos={
            'username_intentado': str(username)[:150] if username else None,
            'proveedor': provider.codigo,
            'motivo': motivo or result.motivo,
        },
    )
