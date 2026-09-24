"""The identity provider contract.

Specification section 3.3: authentication is decoupled from business logic
behind an ``IdentityProvider`` interface. Phase 1 ships only
``LocalIdentityProvider``; phase 2 adds an LDAP/Active Directory implementation
without touching the user model, the authorisation rules or any foreign key,
because every relationship already hangs off the immutable ``users.id`` UUID.

What a future LDAP provider needs, and which already exists:

* ``users.identity_provider`` and ``users.external_id`` -- which directory owns
  the account and its stable identifier there.
* ``users.password_hash`` nullable -- a directory account has no local credential.
* ``role_group_mappings`` -- directory group to internal role, so RBAC stays
  ours even when AD authenticates (section 3.3, last bullet).
* JIT provisioning and scheduled synchronisation hooks, below.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class IdentityRecord:
    """A person as the identity source describes them.

    Deliberately not a ``User``: this is what the *provider* knows, before the
    application decides whether to provision, update or ignore it.
    """

    external_id: str
    username: str
    email: str = None
    nombre: str = None
    apellidos: str = None
    telefono: str = None
    puesto: str = None
    departamento: str = None
    #: Directory group names, mapped to internal roles via role_group_mappings.
    grupos: tuple = ()
    #: True when the directory reports the account as disabled.
    deshabilitado: bool = False
    #: Provider-specific attributes kept for traceability.
    atributos: dict = field(default_factory=dict)

    @property
    def nombre_completo(self):
        """How to call this person on screen, falling back to their login.

        A directory that fills neither name field is unusual and not an error,
        and showing an empty row would be worse than showing «alopez».
        """
        partes = [p for p in (self.nombre, self.apellidos) if p]
        return ' '.join(partes) if partes else self.username


@dataclass(frozen=True)
class AuthResult:
    """The outcome of an authentication attempt.

    ``exito`` alone decides whether to log the user in. ``motivo`` is for the
    audit trail and the logs -- never for the message shown to the user, which
    must stay generic so it cannot be used to enumerate accounts.
    """

    exito: bool
    #: Present on success, or on a failure where the account was identified.
    record: IdentityRecord = None
    motivo: str = None
    #: True when the credential was right but the account may not be used.
    cuenta_inactiva: bool = False
    #: True when a lockout is in force.
    bloqueada: bool = False
    #: True when the provider says the stored hash should be upgraded.
    necesita_rehash: bool = False

    @classmethod
    def ok(cls, record, necesita_rehash=False):
        return cls(exito=True, record=record, necesita_rehash=necesita_rehash)

    @classmethod
    def fail(cls, motivo, record=None, cuenta_inactiva=False, bloqueada=False):
        return cls(
            exito=False,
            record=record,
            motivo=motivo,
            cuenta_inactiva=cuenta_inactiva,
            bloqueada=bloqueada,
        )


class IdentityProvider(ABC):
    """Authenticates a credential and describes the people it knows about."""

    #: Matches an ``IdentityProviderCode`` value.
    codigo = None
    #: Human name shown on the administration screen.
    nombre = None

    @abstractmethod
    def authenticate(self, username, credential):
        """Verify a credential.

        Implementations must take approximately the same time whether or not the
        account exists, so response timing cannot be used to enumerate users.

        Returns:
            An :class:`AuthResult`.
        """

    @abstractmethod
    def get_user(self, external_id):
        """Look up one identity by its stable external identifier.

        Returns:
            An :class:`IdentityRecord`, or None when unknown.
        """

    def find_by_username(self, username):
        """Look up one identity by login name. Optional; defaults to None."""
        return None

    def search(self, query, limit=25):
        """Search the identity source. Optional; defaults to empty."""
        return []

    def supports_password_change(self):
        """True when passwords can be changed through this application."""
        return False

    def change_password(self, external_id, current_credential, new_credential):
        """Change a password. Only meaningful when supported."""
        raise NotImplementedError(
            f'El proveedor {self.codigo} no admite cambio de contraseña.'
        )

    def map_groups_to_roles(self, grupos):
        """Translate directory groups into internal role codes.

        Consults ``role_group_mappings``, so an administrator configures the
        mapping rather than it being compiled in. Returns an empty list when
        nothing matches, and the caller decides the default role -- authorisation
        stays ours (section 3.3).
        """
        if not grupos:
            return []

        from app.models.user import RoleGroupMapping

        mappings = RoleGroupMapping.query.filter_by(
            proveedor=self.codigo, activo=True
        ).all()
        wanted = {str(g).strip().lower() for g in grupos}
        return [
            str(m.role.codigo)
            for m in mappings
            if m.role and str(m.grupo_externo).strip().lower() in wanted
        ]

    def supports_provisioning(self):
        """True when accounts can be created just-in-time from this source."""
        return False

    def sync_batch(self, since=None, limit=500):
        """Yield identities changed since a timestamp, for scheduled sync.

        Phase 2. Returning an empty iterable means "nothing to synchronise",
        which is the correct answer for a local provider.
        """
        return []

    def health_check(self):
        """Report reachability. Returns ``(ok, detalle)``."""
        return True, 'disponible'

    def __repr__(self):
        return f'<{type(self).__name__} {self.codigo}>'
