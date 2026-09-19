"""Local identity provider: usernames and Argon2id password hashes.

Phase 1 of specification section 3.3. Two behaviours matter beyond simply
checking a hash:

* Failed attempts are counted and the account is temporarily locked, which is
  the brute-force control section 3.2 asks for.
* A login naming an unknown account still performs a dummy hash verification, so
  the response time does not reveal which usernames exist.
"""
import logging

from sqlalchemy import func, or_

from app.extensions import db
from app.models.enums import IdentityProviderCode, UserStatus
from app.models.user import User
from app.services.identity.base import AuthResult, IdentityProvider, IdentityRecord
from app.utils.crypto import dummy_verify, hash_password, verify_password
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)


class LocalIdentityProvider(IdentityProvider):
    """Authenticates against the local ``users`` table."""

    codigo = IdentityProviderCode.LOCAL.value
    nombre = 'Usuarios locales'

    def __init__(self, max_attempts=5, lockout_minutes=15):
        self.max_attempts = max_attempts
        self.lockout_minutes = lockout_minutes

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def authenticate(self, username, credential):
        """Verify a username (or email) and password."""
        user = self._lookup(username)

        if user is None or not user.password_hash:
            # Spend the same time as a real verification would.
            dummy_verify()
            return AuthResult.fail('usuario_inexistente')

        if user.is_locked:
            return AuthResult.fail('cuenta_bloqueada', bloqueada=True)

        valid, needs_rehash = verify_password(user.password_hash, credential)
        if not valid:
            self._register_failure(user)
            return AuthResult.fail('credencial_incorrecta')

        if user.is_deleted or user.estado is not UserStatus.ACTIVO:
            # The credential was right, but the account may not be used. Recorded
            # distinctly for the audit trail; the user still sees a generic message.
            return AuthResult.fail('cuenta_inactiva', cuenta_inactiva=True)

        self._register_success(user)
        return AuthResult.ok(self._to_record(user), necesita_rehash=needs_rehash)

    def _lookup(self, username):
        """Find a local account by username or email, case-insensitively."""
        if not username:
            return None
        needle = str(username).strip().lower()
        return User.query.filter(
            User.identity_provider == IdentityProviderCode.LOCAL.value,
            User.is_deleted.is_(False),
            or_(
                func.lower(User.username) == needle,
                func.lower(User.email) == needle,
            ),
        ).first()

    def _register_failure(self, user):
        """Count the failure and lock the account once the limit is reached."""
        from datetime import timedelta

        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= self.max_attempts:
            user.locked_until = utcnow() + timedelta(minutes=self.lockout_minutes)
            logger.warning(
                'Cuenta bloqueada por intentos fallidos: %s (hasta %s)',
                user.username, user.locked_until,
            )
        db.session.commit()

    def _register_success(self, user):
        """Clear the failure counter and stamp the login."""
        from flask import has_request_context, request

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = utcnow()
        if has_request_context():
            user.last_login_ip = request.remote_addr
        db.session.commit()

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def get_user(self, external_id):
        """For local accounts the external id is the UUID itself."""
        import uuid

        try:
            user = db.session.get(User, uuid.UUID(str(external_id)))
        except (ValueError, TypeError):
            return None
        return self._to_record(user) if user else None

    def find_by_username(self, username):
        user = self._lookup(username)
        return self._to_record(user) if user else None

    def search(self, query, limit=25):
        """Search local accounts by name, username or email."""
        if not query:
            return []
        pattern = f'%{str(query).strip().lower()}%'
        users = User.query.filter(
            User.is_deleted.is_(False),
            or_(
                func.lower(User.username).like(pattern),
                func.lower(User.email).like(pattern),
                func.lower(User.nombre).like(pattern),
                func.lower(User.apellidos).like(pattern),
            ),
        ).limit(limit).all()
        return [self._to_record(u) for u in users]

    # ------------------------------------------------------------------
    # Passwords
    # ------------------------------------------------------------------
    def supports_password_change(self):
        return True

    def change_password(self, external_id, current_credential, new_credential):
        """Change a local password, invalidating every existing session."""
        import uuid

        from app.utils.errors import AuthenticationFailed, ValidationError

        user = db.session.get(User, uuid.UUID(str(external_id)))
        if user is None:
            raise AuthenticationFailed()

        if current_credential is not None:
            valid, _ = verify_password(user.password_hash, current_credential)
            if not valid:
                raise AuthenticationFailed('La contraseña actual no es correcta.')

        problems = validate_password_strength(new_credential)
        if problems:
            raise ValidationError(problems[0], detalles={'requisitos': problems})

        user.password_hash = hash_password(new_credential)
        user.password_changed_at = utcnow()
        user.must_change_password = False
        # Force every other session for this user to re-authenticate.
        user.bump_session_epoch()
        db.session.commit()
        return True

    def supports_provisioning(self):
        """Local accounts are created deliberately by an administrator."""
        return False

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------
    @staticmethod
    def _to_record(user):
        """Project a ``User`` row onto the provider-neutral record."""
        return IdentityRecord(
            external_id=str(user.id),
            username=user.username,
            email=user.email,
            nombre=user.nombre,
            apellidos=user.apellidos,
            telefono=user.telefono,
            puesto=user.puesto,
            departamento=user.departamento,
            grupos=tuple(user.role_codes),
            deshabilitado=(user.estado is not UserStatus.ACTIVO) or user.is_deleted,
        )


#: Minimum length required of a local password.
MIN_PASSWORD_LENGTH = 12


def validate_password_strength(password):
    """Check a candidate password, returning a list of Spanish problems.

    An empty list means the password is acceptable. Length is weighted most
    heavily because it is what actually resists offline cracking.
    """
    problems = []
    if not password:
        return ['La contraseña no puede estar vacía.']
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(
            f'La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres.'
        )
    if not any(c.islower() for c in password):
        problems.append('La contraseña debe incluir alguna letra minúscula.')
    if not any(c.isupper() for c in password):
        problems.append('La contraseña debe incluir alguna letra mayúscula.')
    if not any(c.isdigit() for c in password):
        problems.append('La contraseña debe incluir algún dígito.')
    if password.lower() in _COMMON_PASSWORDS:
        problems.append('La contraseña es demasiado común.')
    return problems


#: A short deny-list. A real deployment should point at a corporate list or a
#: breached-password service; this catches the most careless choices.
_COMMON_PASSWORDS = frozenset({
    'password', 'password123', 'contraseña', 'contrasena123', '123456789012',
    'qwertyuiop12', 'administrador', 'travelmanager', 'gestorviajes1',
})
