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
from app.services.identity.ldap import LDAPIdentityProvider
from app.services.identity.local import LocalIdentityProvider
from app.utils.errors import AccountLocked, AuthenticationFailed, ValidationError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Registered provider classes, keyed by their code.
_PROVIDERS = {
    LocalIdentityProvider.codigo: LocalIdentityProvider,
    LDAPIdentityProvider.codigo: LDAPIdentityProvider,
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
    provider = get_provider(provider_code or _provider_for(username))
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


def _provider_for(username):
    """Decide which directory owns a login name.

    The corporate directory does not replace local accounts; both work at once.
    So the question is not «which provider is configured» but «whose account is
    this», and the account itself answers it.

    Resolved before attempting anything rather than by trying one and falling
    back to the other. A fallback would write a failed-login audit entry for
    every directory user on every successful login, and the trail is the thing
    somebody reads after an incident -- filling it with failures that were not
    failures is how it stops being read.

    An unknown name goes to the directory when one is configured, because that
    is the only provider that can create an account for somebody who has never
    logged in. When none is, it goes to the local provider and fails there, the
    same way and in the same time as any wrong password.
    """
    from app.services.identity.ldap import esta_configurado

    if not username:
        return None

    texto = str(username).strip()
    existente = User.query.filter(
        db.or_(User.username == texto, User.email == texto.lower()),
        User.is_deleted.is_(False),
    ).first()
    if existente is not None:
        return existente.identity_provider

    if esta_configurado():
        return IdentityProviderCode.LDAP.value
    return None


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
        if not _jit_permitido(provider):
            logger.warning(
                'Identidad %s autenticada pero el aprovisionamiento JIT está desactivado.',
                record.username,
            )
            return None
        user = provision_user(provider, record)

    elif user is not None and provider.supports_provisioning():
        _sync_attributes(provider, user, record)

    return user


def _jit_permitido(provider):
    """Whether a directory account may be created on first login.

    Turning the directory on in Ajustes *is* the decision. A second switch in
    the environment saying «and I meant it» would sit there forgotten, and the
    symptom would be somebody authenticating correctly and being refused with
    no explanation -- the directory said yes, the application said no, and
    nothing on screen says why.

    The environment value stays as the answer for any other provider, and as
    the bootstrap on a fresh install.
    """
    from app.services.identity.ldap import esta_configurado

    if provider.codigo == IdentityProviderCode.LDAP.value:
        return esta_configurado()
    return bool(current_app.config['IDENTITY_JIT_PROVISIONING'])


def _proveedor_del_directorio():
    """The corporate directory, or None when there is not one.

    Asked for by name rather than through ``get_provider()``: that one answers
    with whatever ``IDENTITY_PROVIDER`` says, which is the local provider in
    every installation that did not switch it -- and the directory is chosen
    per account, not globally. Asking the default here made the search return
    nothing and look like an empty directory.
    """
    from app.services.identity.ldap import esta_configurado

    if not esta_configurado():
        return None
    return get_provider(IdentityProviderCode.LDAP.value)


def buscar_en_el_directorio(texto, limite=10):
    """People the directory knows, whether or not they exist here yet.

    Somebody who has never signed in has no local account, so they could not be
    added to a trip at all -- and in an organisation where the directory is the
    payroll, that is most of the staff on the day the system is installed.

    Returns ``(record, user_or_None)`` pairs so the caller can tell «ya está»
    from «habría que darle de alta» without asking twice.
    """
    from app.models.user import User

    provider = _proveedor_del_directorio()
    if provider is None:
        return []

    try:
        registros = provider.search(texto, limit=limite) or []
    except Exception:
        # Un directorio caído no puede romper la pantalla de viajeros: lo que
        # ya está en local se sigue pudiendo asignar.
        logger.warning('No se pudo buscar «%s» en el directorio.', texto)
        return []

    salida = []
    for record in registros:
        local = User.query.filter_by(
            identity_provider=provider.codigo, external_id=record.external_id,
        ).first() if record.external_id else None
        if local is None:
            local = User.query.filter_by(username=record.username).first()
        salida.append((record, local))
    return salida


def dar_de_alta_desde_el_directorio(texto_identificador):
    """Create the local account for one person the directory knows.

    Called when somebody is put on a trip, not when they are merely listed: a
    search must not fill the user table with everybody who matched «gar».

    Idempotent -- asked twice it returns the same account rather than a second
    one, because two rows for one person split their trips in half.
    """
    from app.models.user import User

    provider = _proveedor_del_directorio()
    if provider is None:
        raise ValidationError('No hay ningún directorio configurado.')

    record = _registro_del_directorio(provider, texto_identificador)
    if record is None:
        raise ValidationError(
            'El directorio ya no reconoce a esa persona. Vuelva a buscarla.'
        )

    existente = User.query.filter_by(
        identity_provider=provider.codigo, external_id=record.external_id,
    ).first() if record.external_id else None
    if existente is None:
        existente = User.query.filter_by(username=record.username).first()
    if existente is not None:
        return existente

    user = provision_user(provider, record)
    db.session.commit()
    logger.info('Alta desde el directorio: %s', user.username)
    return user


def _registro_del_directorio(provider, identificador):
    """Find one person again, by whatever the screen sent back.

    The stable identifier first -- ``get_user`` looks a person up by the
    directory's own GUID, and a login name can be changed there without
    anything here noticing. But not every directory exposes one, so a search by
    name is the fallback, accepted only on an exact match: «ana» must not
    provision the first of eleven Anas.
    """
    if not identificador:
        return None

    record = provider.get_user(identificador)
    if record is not None:
        return record

    texto = str(identificador).strip().lower()
    for candidato in provider.search(identificador, limit=25) or []:
        if (candidato.username or '').strip().lower() == texto:
            return candidato
    return None


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
