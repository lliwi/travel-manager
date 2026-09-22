"""Users, roles and the organisational scope placeholder.

Specification section 3.3 is the driver here: the identity *provider* is
pluggable, the internal identifier is an immutable UUID, and the login name is
never a business key. Everything an LDAP/AD provider will need later
(``identity_provider``, ``external_id``, nullable ``password_hash``, the group
to role mapping table) exists now so phase 2 is a configuration change rather
than a migration of every foreign key.
"""
from flask_login import UserMixin
from sqlalchemy import UniqueConstraint, func

from app.extensions import db
from app.models.base import (
    GUID,
    BaseModel,
    JSONBType,
    SoftDeleteMixin,
    enum_check,
    enum_column,
)
from app.models.enums import IdentityProviderCode, RoleCode, UserStatus
from app.utils.timeutil import utcnow

#: RBAC assignment table. Kept as a plain association table because a role
#: assignment carries no attributes of its own.
user_roles = db.Table(
    'user_roles',
    db.Column('user_id', GUID(), db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    db.Column('role_id', GUID(), db.ForeignKey('roles.id', ondelete='CASCADE'), primary_key=True),
    db.Column('assigned_at', db.DateTime(timezone=True), nullable=False, default=utcnow),
    db.Column('assigned_by_id', GUID(), db.ForeignKey('users.id'), nullable=True),
)


class Organization(BaseModel):
    """Organisational scope.

    Specification section 2.1 states that a manager administers every trip
    "salvo que se configure en el futuro un ámbito organizativo". This table
    exists with a single seeded row and nullable references so that future scope
    is a filter change rather than a backfill across every user and trip. It is
    the only speculative schema in the model, and it is deliberate.
    """

    __tablename__ = 'organizations'

    nombre = db.Column(db.String(200), nullable=False)
    codigo = db.Column(db.String(50), nullable=False, unique=True, index=True)
    activa = db.Column(db.Boolean, nullable=False, default=True)
    zona_horaria = db.Column(db.String(64), nullable=False, default='Europe/Madrid')

    def __repr__(self):
        return f'<Organization {self.codigo}>'


class Role(BaseModel):
    """One of the three access profiles of specification section 2.1."""

    __tablename__ = 'roles'
    __table_args__ = (enum_check('codigo', RoleCode, 'roles'),)

    codigo = enum_column(RoleCode, nullable=False, index=True, unique=True)
    nombre = db.Column(db.String(100), nullable=False)
    descripcion = db.Column(db.Text)
    #: Built-in roles cannot be deleted or renamed by an administrator.
    es_sistema = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f'<Role {self.codigo}>'

    @classmethod
    def get(cls, code):
        """Fetch a role by its code, accepting either the enum or the string."""
        value = code.value if isinstance(code, RoleCode) else str(code)
        return cls.query.filter_by(codigo=value).first()


class RoleGroupMapping(BaseModel):
    """Maps an external directory group to an internal role.

    Empty in phase 1. Phase 2 populates it so AD group membership can drive
    role assignment without delegating authorisation itself -- section 3.3 keeps
    RBAC internal even once AD authenticates.
    """

    __tablename__ = 'role_group_mappings'
    __table_args__ = (
        UniqueConstraint('proveedor', 'grupo_externo', name='uq_role_group_mappings_proveedor_grupo'),
        enum_check('proveedor', IdentityProviderCode, 'role_group_mappings'),
    )

    proveedor = enum_column(
        IdentityProviderCode, nullable=False, default=IdentityProviderCode.LDAP
    )
    grupo_externo = db.Column(db.String(400), nullable=False)
    role_id = db.Column(GUID(), db.ForeignKey('roles.id', ondelete='CASCADE'), nullable=False)
    activo = db.Column(db.Boolean, nullable=False, default=True)

    role = db.relationship('Role', lazy='joined')


class MFARecoveryCode(BaseModel):
    """One single-use code for getting back in without the authenticator.

    Stored hashed, like a password, because that is what it is: anybody who
    reads the table would otherwise hold a permanent bypass of the second
    factor. Consumed rather than deleted, so «somebody used a recovery code»
    stays answerable after the fact -- that is the signal that a phone was
    lost, or that somebody else has the codes.
    """

    __tablename__ = 'mfa_recovery_codes'

    user_id = db.Column(
        GUID(), db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    code_hash = db.Column(db.String(255), nullable=False)
    usado_en = db.Column(db.DateTime(timezone=True))

    user = db.relationship('User', back_populates='mfa_recovery_codes')

    @property
    def disponible(self):
        return self.usado_en is None

    def __repr__(self):
        estado = 'usado' if self.usado_en else 'disponible'
        return f'<MFARecoveryCode {self.user_id} {estado}>'


class User(UserMixin, SoftDeleteMixin, BaseModel):
    """An account, local or directory-backed.

    ``id`` is the immutable UUID every other table references. ``username`` and
    ``email`` can change; they are never used as a business key.
    """

    __tablename__ = 'users'
    __table_args__ = (
        UniqueConstraint('identity_provider', 'external_id', name='uq_users_identity_provider_external_id'),
        enum_check('estado', UserStatus, 'users'),
        enum_check('identity_provider', IdentityProviderCode, 'users'),
        db.Index('ix_users_email_lower', func.lower(db.text('email'))),
    )

    # --- Identity -----------------------------------------------------
    username = db.Column(db.String(150), nullable=False, unique=True, index=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    identity_provider = enum_column(
        IdentityProviderCode, nullable=False, default=IdentityProviderCode.LOCAL, index=True
    )
    #: Stable identifier in the external directory (objectGUID / UPN). Null for
    #: local accounts.
    external_id = db.Column(db.String(255), nullable=True, index=True)
    #: Null for directory-backed accounts, which have no local credential.
    password_hash = db.Column(db.String(255), nullable=True)
    password_changed_at = db.Column(db.DateTime(timezone=True))
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)

    # --- Second factor (TOTP, RFC 6238) -------------------------------
    #: Encrypted with the same key as the AI provider credentials. A TOTP
    #: secret is a password equivalent: whoever reads it can generate valid
    #: codes for ever, so it never sits in a column in the clear.
    mfa_secret_cifrado = db.Column(db.Text)
    #: Set only once the person has proved they can produce a code. An enrolled
    #: secret that was never confirmed would lock somebody out of their own
    #: account with a authenticator that was mis-scanned.
    mfa_activado_en = db.Column(db.DateTime(timezone=True))
    #: The step the last accepted code belonged to. TOTP accepts a code for a
    #: whole 30-second window, so without this the same code works twice --
    #: which is the entire value of anybody who reads it over a shoulder.
    mfa_ultimo_paso = db.Column(db.BigInteger)

    # --- Profile ------------------------------------------------------
    nombre = db.Column(db.String(150), nullable=False)
    apellidos = db.Column(db.String(200))
    telefono = db.Column(db.String(50))
    puesto = db.Column(db.String(150))
    departamento = db.Column(db.String(150))
    idioma = db.Column(db.String(10), nullable=False, default='es')
    zona_horaria = db.Column(db.String(64), nullable=False, default='Europe/Madrid')

    # --- Status -------------------------------------------------------
    estado = enum_column(UserStatus, nullable=False, default=UserStatus.ACTIVO, index=True)
    last_login_at = db.Column(db.DateTime(timezone=True))
    last_login_ip = db.Column(db.String(45))
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))
    #: Bumped to invalidate every existing session for this user at once
    #: (password change, deactivation, role change).
    session_epoch = db.Column(db.Integer, nullable=False, default=0)

    # --- Scope (unused in phase 1, see Organization) -------------------
    organizacion_id = db.Column(GUID(), db.ForeignKey('organizations.id'), nullable=True)

    # --- Directory sync metadata --------------------------------------
    external_attributes = db.Column(JSONBType())
    synced_at = db.Column(db.DateTime(timezone=True))

    # --- Relationships ------------------------------------------------
    roles = db.relationship(
        'Role',
        secondary=user_roles,
        primaryjoin='User.id == user_roles.c.user_id',
        secondaryjoin='Role.id == user_roles.c.role_id',
        backref=db.backref('users', lazy='dynamic'),
        lazy='selectin',
    )
    organizacion = db.relationship('Organization', lazy='joined')
    mfa_recovery_codes = db.relationship(
        'MFARecoveryCode',
        back_populates='user',
        cascade='all, delete-orphan',
        lazy='selectin',
    )

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    @property
    def nombre_completo(self):
        """Full name for display, falling back to the username."""
        parts = [p for p in (self.nombre, self.apellidos) if p]
        return ' '.join(parts) if parts else self.username

    @property
    def iniciales(self):
        """Two-letter initials used by the avatar chip."""
        first = (self.nombre or self.username or '?')[:1]
        second = (self.apellidos or '')[:1]
        return (first + second).upper() or '?'

    def __repr__(self):
        return f'<User {self.username}>'

    # ------------------------------------------------------------------
    # Flask-Login contract
    # ------------------------------------------------------------------
    def get_id(self):
        """Session identity: the UUID plus the session epoch.

        Encoding the epoch means bumping ``session_epoch`` invalidates every
        outstanding session for this user without touching the session store.
        """
        return f'{self.id}:{self.session_epoch}'

    @property
    def is_active(self):
        """Flask-Login uses this to refuse login for disabled accounts."""
        return self.is_active_account

    @property
    def is_active_account(self):
        """True when the account may authenticate and hold a session."""
        if self.is_deleted or self.estado is not UserStatus.ACTIVO:
            return False
        return not self.is_locked

    @property
    def is_locked(self):
        """True while a temporary lockout from failed logins is in force.

        The stored value is normalised before comparing: PostgreSQL returns a
        timezone-aware datetime from TIMESTAMPTZ while SQLite returns a naive
        one, and comparing the two raises. Every writer stores UTC, so a naive
        value is read as UTC.
        """
        if self.locked_until is None:
            return False

        limite = self.locked_until
        if limite.tzinfo is None:
            from datetime import timezone

            limite = limite.replace(tzinfo=timezone.utc)
        return limite > utcnow()

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------
    def has_role(self, role_code):
        """True when this user holds the given role."""
        value = role_code.value if isinstance(role_code, RoleCode) else str(role_code)
        return any(str(role.codigo) == value for role in self.roles)

    def has_any_role(self, *role_codes):
        return any(self.has_role(code) for code in role_codes)

    @property
    def mfa_activo(self):
        """True when a confirmed second factor guards this account.

        Confirmed, not merely enrolled: a secret somebody scanned wrong and
        never verified would lock them out of their own account, so the switch
        is the confirmation timestamp and not the presence of a secret.
        """
        return bool(self.mfa_secret_cifrado and self.mfa_activado_en)

    @property
    def mfa_codigos_disponibles(self):
        """How many recovery codes are still unused."""
        return sum(1 for c in self.mfa_recovery_codes if c.disponible)

    @property
    def es_local(self):
        """True when this account's credential lives here.

        The two kinds coexist, so «which one is this» is a question the screens
        and the login path both ask, and it should have one answer rather than
        a comparison repeated at each of them.
        """
        return str(self.identity_provider) == IdentityProviderCode.LOCAL.value

    @property
    def origen(self):
        """Where this account comes from, for the administration screens."""
        proveedor = IdentityProviderCode.coerce(self.identity_provider)
        return proveedor.label if proveedor else str(self.identity_provider)

    @property
    def is_administrador(self):
        return self.has_role(RoleCode.ADMINISTRADOR)

    @property
    def is_gestor(self):
        return self.has_role(RoleCode.GESTOR)

    @property
    def role_codes(self):
        return sorted(str(role.codigo) for role in self.roles)

    def add_role(self, role, actor=None):
        """Grant a role, invalidating existing sessions so it takes effect."""
        if role is not None and role not in self.roles:
            self.roles.append(role)
            self.bump_session_epoch()
        return self

    def remove_role(self, role):
        """Revoke a role, invalidating existing sessions."""
        if role is not None and role in self.roles:
            self.roles.remove(role)
            self.bump_session_epoch()
        return self

    def bump_session_epoch(self):
        """Invalidate every session currently held by this user."""
        self.session_epoch = (self.session_epoch or 0) + 1
        return self

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------
    def to_dict(self, include_roles=True):
        """API representation. Never includes the password hash."""
        data = {
            'id': str(self.id),
            'username': self.username,
            'email': self.email,
            'nombre': self.nombre,
            'apellidos': self.apellidos,
            'nombre_completo': self.nombre_completo,
            'puesto': self.puesto,
            'departamento': self.departamento,
            'estado': str(self.estado),
            'identity_provider': str(self.identity_provider),
            'idioma': self.idioma,
            'zona_horaria': self.zona_horaria,
            'last_login_at': self.last_login_at.isoformat() if self.last_login_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if include_roles:
            data['roles'] = self.role_codes
        return data

    def to_reference(self):
        """Minimal representation for embedding in other payloads."""
        return {
            'id': str(self.id),
            'nombre_completo': self.nombre_completo,
            'email': self.email,
        }
