"""Immutable audit trail.

Specification section 2.1 requires sensitive operations to be audited, and
section 3.2 requires the trail to be immutable or integrity-controlled. Three
mechanisms enforce that together:

1. ORM ``before_update`` / ``before_delete`` hooks that raise.
2. A hash chain: each row hashes its own content plus the previous row's hash,
   so removing or editing a row at the database level is detectable.
3. No soft-delete mixin and no update path in any service.

The primary key is ``BIGSERIAL`` rather than the UUID used everywhere else. This
is a deliberate, documented departure from section 4: the table is an append-only
log that no business foreign key references, and a monotonic key gives ordered
inserts, a compact index, cheap keyset pagination and a natural chain order.
"""
import hashlib
import json
from datetime import UTC

from sqlalchemy import event

from app.extensions import db
from app.models.base import GUID, EnumType, JSONBType, enum_check
from app.models.enums import AuditResourceType, AuditResult
from app.utils.timeutil import utcnow

#: Written into ``hash_anterior`` for the very first row of the chain.
GENESIS_HASH = '0' * 64


def _canonical_timestamp(value):
    """Render a timestamp identically however the driver returned it.

    PostgreSQL gives back a timezone-aware value from TIMESTAMPTZ while SQLite
    gives back a naive one. If the digest depended on that difference, every
    row would appear tampered with after a round-trip through the database and
    the integrity check would be useless. A naive value is read as UTC, which
    is what every writer stores.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime('%Y-%m-%dT%H:%M:%S.%f')


class AuditEvent(db.Model):
    """One recorded action. Append-only."""

    __tablename__ = 'audit_events'
    __table_args__ = (
        db.Index('ix_audit_events_actor_created', 'actor_id', 'created_at'),
        db.Index('ix_audit_events_recurso', 'recurso_tipo', 'recurso_id'),
        db.Index('ix_audit_events_accion_created', 'accion', 'created_at'),
        enum_check('resultado', AuditResult, 'audit_events'),
    )

    # BIGSERIAL on PostgreSQL. SQLite only auto-increments an INTEGER PRIMARY
    # KEY (it aliases rowid), so the variant keeps the test suite working
    # without weakening the production column.
    id = db.Column(
        db.BigInteger().with_variant(db.Integer, 'sqlite'),
        primary_key=True,
        autoincrement=True,
    )

    # --- Who ----------------------------------------------------------
    actor_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True, index=True)
    #: Denormalised so the trail stays readable after an account is renamed or
    #: erased under a retention policy.
    actor_username = db.Column(db.String(150))

    # --- What ---------------------------------------------------------
    accion = db.Column(db.String(100), nullable=False, index=True)
    recurso_tipo = db.Column(EnumType(AuditResourceType), nullable=True, index=True)
    recurso_id = db.Column(db.String(64), nullable=True, index=True)
    resultado = db.Column(
        EnumType(AuditResult), nullable=False, default=AuditResult.EXITO, index=True
    )

    # --- Context ------------------------------------------------------
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(400))
    request_method = db.Column(db.String(10))
    request_path = db.Column(db.String(500))
    correlation_id = db.Column(db.String(64), index=True)

    #: Minimised metadata. Never store document contents, credentials or full
    #: personal records here -- section 3.2 requires logs free of unnecessary PII.
    metadatos = db.Column(JSONBType())

    # --- When ---------------------------------------------------------
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    # --- Integrity chain ----------------------------------------------
    hash_anterior = db.Column(db.String(64), nullable=False, default=GENESIS_HASH)
    hash_propio = db.Column(db.String(64), nullable=False)

    actor = db.relationship('User', foreign_keys=[actor_id], lazy='joined')

    def __repr__(self):
        return f'<AuditEvent {self.id} {self.accion} {self.resultado}>'

    # ------------------------------------------------------------------
    # Hash chain
    # ------------------------------------------------------------------
    def compute_hash(self):
        """Derive this row's hash from its content and the previous hash.

        The payload is serialised with sorted keys so the digest is stable
        across Python versions and dict ordering.
        """
        payload = {
            'actor_id': str(self.actor_id) if self.actor_id else None,
            'actor_username': self.actor_username,
            'accion': self.accion,
            'recurso_tipo': str(self.recurso_tipo) if self.recurso_tipo else None,
            'recurso_id': self.recurso_id,
            'resultado': str(self.resultado) if self.resultado else None,
            'ip_address': self.ip_address,
            'request_method': self.request_method,
            'request_path': self.request_path,
            'correlation_id': self.correlation_id,
            'metadatos': self.metadatos,
            'created_at': _canonical_timestamp(self.created_at),
            'hash_anterior': self.hash_anterior,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def verify(self, previous_hash=None):
        """True when this row's stored hash still matches its content."""
        if previous_hash is not None and self.hash_anterior != previous_hash:
            return False
        return self.hash_propio == self.compute_hash()

    def to_dict(self):
        """API representation for ``GET /api/v1/audit-events``."""
        return {
            'id': self.id,
            'actor': {
                'id': str(self.actor_id) if self.actor_id else None,
                'username': self.actor_username,
            },
            'accion': self.accion,
            'recurso_tipo': str(self.recurso_tipo) if self.recurso_tipo else None,
            'recurso_id': self.recurso_id,
            'resultado': str(self.resultado) if self.resultado else None,
            'ip_address': self.ip_address,
            'request_path': self.request_path,
            'correlation_id': self.correlation_id,
            'metadatos': self.metadatos or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'hash_propio': self.hash_propio,
        }


# ----------------------------------------------------------------------
# Immutability guards
# ----------------------------------------------------------------------
@event.listens_for(AuditEvent, 'before_update')
def _prevent_audit_update(mapper, connection, target):
    """Audit rows are immutable (specification section 3.2)."""
    raise RuntimeError('Los eventos de auditoría son inmutables y no pueden modificarse.')


@event.listens_for(AuditEvent, 'before_delete')
def _prevent_audit_delete(mapper, connection, target):
    """Audit rows cannot be deleted through the ORM.

    Retention-driven purging is a separate, explicitly privileged operation
    that runs at the database level and records its own audit entry.
    """
    raise RuntimeError('Los eventos de auditoría no pueden eliminarse.')
