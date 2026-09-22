"""Shared model mixins and column helpers.

Specification section 3.3 requires an immutable UUID business key, so every
entity gets one from :class:`UUIDMixin` rather than an auto-increment integer.
The one deliberate exception is ``audit_events``, an append-only log that no
business foreign key ever references and that benefits from an ordered,
compact key -- see ``app/models/audit.py``.
"""
import uuid

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import CHAR, TypeDecorator

from app.extensions import db
from app.utils.timeutil import utcnow


class GUID(TypeDecorator):
    """Platform-independent UUID column.

    Uses PostgreSQL's native ``UUID`` type where available and falls back to a
    36-character string elsewhere, which is what lets the test suite run against
    SQLite while production runs on PostgreSQL.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == 'postgresql':
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class JSONBType(TypeDecorator):
    """``JSONB`` on PostgreSQL, plain JSON elsewhere.

    Specification section 4: JSONB is for provider- or document-specific
    variable fields only. Anything filtered, validated or related frequently
    gets a typed column instead.
    """

    impl = db.JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(db.JSON())


@compiles(db.DateTime, 'postgresql')
def _compile_datetime_pg(type_, compiler, **kw):
    """Emit TIMESTAMPTZ for timezone-aware datetime columns on PostgreSQL."""
    return 'TIMESTAMP WITH TIME ZONE' if type_.timezone else 'TIMESTAMP WITHOUT TIME ZONE'


def uuid_pk():
    """A UUID primary key column with a Python-side default."""
    return db.Column(GUID(), primary_key=True, default=uuid.uuid4)


def uuid_fk(target, nullable=True, index=True, ondelete=None):
    """A UUID foreign key column."""
    return db.Column(
        GUID(),
        db.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
        index=index,
    )


def utc_column(nullable=True, index=False, default=None):
    """A timezone-aware UTC timestamp column."""
    return db.Column(
        db.DateTime(timezone=True), nullable=nullable, index=index, default=default
    )


class UUIDMixin:
    """Immutable UUID primary key (specification sections 3.3 and 4)."""

    @db.declared_attr
    def id(cls):
        return db.Column(GUID(), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Creation and update timestamps, always timezone-aware UTC."""

    @db.declared_attr
    def created_at(cls):
        return db.Column(
            db.DateTime(timezone=True), nullable=False, default=utcnow, index=True
        )

    @db.declared_attr
    def updated_at(cls):
        return db.Column(
            db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
        )


class SoftDeleteMixin:
    """Soft deletion.

    Records are never hard-deleted: the audit trail, provenance chain and
    historical alert evidence all reference them, and section 3.2 requires that
    trail to stay intact. Retention-driven erasure is a separate, explicit
    operation handled by the retention job.
    """

    @db.declared_attr
    def is_deleted(cls):
        return db.Column(db.Boolean, nullable=False, default=False, index=True)

    @db.declared_attr
    def deleted_at(cls):
        return db.Column(db.DateTime(timezone=True), nullable=True)

    @db.declared_attr
    def deleted_by_id(cls):
        return db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    def soft_delete(self, actor=None):
        """Mark this record deleted, recording who did it and when."""
        self.is_deleted = True
        self.deleted_at = utcnow()
        self.deleted_by_id = getattr(actor, 'id', None)
        return self

    def restore(self):
        """Undo a soft deletion."""
        self.is_deleted = False
        self.deleted_at = None
        self.deleted_by_id = None
        return self

    @classmethod
    def alive(cls):
        """Query restricted to records that are not soft-deleted."""
        return cls.query.filter(cls.is_deleted.is_(False))


class InstantMixin:
    """Helpers for entities carrying ``_local`` / ``_tz`` / ``_utc`` triples."""

    def set_instant(self, prefix, local_dt, tz_name):
        """Write an instant triple. The only supported way to set one."""
        from app.utils.timeutil import set_instant as _set

        return _set(self, prefix, local_dt, tz_name)

    def get_instant(self, prefix):
        """Read an instant triple as ``(local, tz_name, utc)``."""
        from app.utils.timeutil import get_instant as _get

        return _get(self, prefix)


class BaseModel(UUIDMixin, TimestampMixin, db.Model):
    """Abstract base for entities with a UUID key and timestamps."""

    __abstract__ = True

    def __repr__(self):
        return f'<{type(self).__name__} {self.id}>'


class EnumType(TypeDecorator):
    """Store a :class:`~app.models.enums.LabeledEnum` as its short string value.

    A native PostgreSQL ENUM would need a migration for every new member, and
    the document and alert state machines will keep acquiring them. A VARCHAR
    plus a table-level CHECK constraint gives the same integrity for a one-line
    migration, and reading back yields the enum member, not a bare string.
    """

    impl = db.String
    cache_ok = True

    def __init__(self, enum_cls, length=40, **kwargs):
        self.enum_cls = enum_cls
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return value.value
        # Accept a raw string so filters like `Trip.estado == 'confirmado'` work.
        return self.enum_cls(value).value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return self.enum_cls.coerce(value, None)


def enum_column(enum_cls, nullable=False, default=None, index=False, length=40, **kwargs):
    """An enum-backed column with a Python-side default.

    The matching CHECK constraint is added per table by :func:`enum_check`, so
    the database rejects values the application never produces.
    """
    return db.Column(
        EnumType(enum_cls, length=length),
        nullable=nullable,
        default=default,
        index=index,
        **kwargs,
    )


def enum_check(column_name, enum_cls, table_name=None):
    """CHECK constraint restricting a column to an enum's values.

    The name is the bare suffix: the ``ck`` naming convention in
    ``app.extensions`` prepends ``ck_<table>_``, so including the prefix here
    would produce ``ck_trips_ck_trips_estado_valido``.

    ``table_name`` is accepted and ignored, so call sites read explicitly.
    """
    allowed = ', '.join(f"'{v}'" for v in enum_cls.values())
    return db.CheckConstraint(
        f'{column_name} IN ({allowed})',
        name=f'{column_name}_valido',
    )
