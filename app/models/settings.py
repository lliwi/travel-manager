"""Runtime settings an administrator can change without a redeploy.

Specification section 10 lists decisions the organisation had not yet made when
the requirement was written (retention windows, authorised sources, supported
languages, thresholds). Those belong here rather than in ``config.py``: they are
policy, not deployment configuration.
"""
from sqlalchemy import UniqueConstraint

from app.extensions import db
from app.models.base import GUID, BaseModel, JSONBType


class SystemSetting(BaseModel):
    """One administrator-editable setting."""

    __tablename__ = 'system_settings'
    __table_args__ = (
        UniqueConstraint('clave', name='uq_system_settings_clave'),
    )

    clave = db.Column(db.String(120), nullable=False, index=True)
    valor = db.Column(JSONBType())
    tipo = db.Column(db.String(20), nullable=False, default='string')
    grupo = db.Column(db.String(60), index=True)
    nombre = db.Column(db.String(200))
    descripcion = db.Column(db.Text)
    #: False for settings only an administrator should see, e.g. egress policy.
    visible_gestor = db.Column(db.Boolean, nullable=False, default=False)
    actualizado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    actualizado_por = db.relationship('User', foreign_keys=[actualizado_por_id], lazy='select')

    def __repr__(self):
        return f'<SystemSetting {self.clave}>'

    def to_dict(self):
        return {
            'clave': self.clave,
            'valor': self.valor,
            'tipo': self.tipo,
            'grupo': self.grupo,
            'nombre': self.nombre,
            'descripcion': self.descripcion,
            'actualizado_en': self.updated_at.isoformat() if self.updated_at else None,
        }


class TravelerDocument(BaseModel):
    """A traveller's passport, visa or insurance policy.

    Behind the ``TRAVELER_DOCUMENTS_ENABLED`` flag (off by default). Keyed to the
    *user*, not the trip: a passport belongs to a person and its expiry matters
    across every trip they take. The number itself is encrypted at rest; only the
    last four characters are stored in clear for display.
    """

    __tablename__ = 'traveler_documents'
    __table_args__ = (
        db.Index('ix_traveler_documents_user_tipo', 'user_id', 'tipo'),
        db.Index('ix_traveler_documents_caducidad', 'fecha_caducidad'),
    )

    user_id = db.Column(
        GUID(), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True
    )
    tipo = db.Column(db.String(40), nullable=False)
    pais_emisor = db.Column(db.String(2))
    #: Fernet-encrypted document number.
    numero_encrypted = db.Column(db.Text)
    numero_ultimos4 = db.Column(db.String(4))
    fecha_emision = db.Column(db.Date)
    fecha_caducidad = db.Column(db.Date, index=True)
    notas = db.Column(db.Text)
    activo = db.Column(db.Boolean, nullable=False, default=True)

    user = db.relationship('User', foreign_keys=[user_id], lazy='joined')

    def __repr__(self):
        return f'<TravelerDocument {self.tipo} ****{self.numero_ultimos4}>'

    def dias_hasta_caducidad(self, reference=None):
        """Days remaining before expiry, negative when already expired."""
        if not self.fecha_caducidad:
            return None
        from datetime import date

        return (self.fecha_caducidad - (reference or date.today())).days

    def to_dict(self):
        return {
            'id': str(self.id),
            'tipo': self.tipo,
            'pais_emisor': self.pais_emisor,
            'numero': f'****{self.numero_ultimos4}' if self.numero_ultimos4 else None,
            'fecha_caducidad': self.fecha_caducidad.isoformat() if self.fecha_caducidad else None,
            'dias_hasta_caducidad': self.dias_hasta_caducidad(),
            'activo': self.activo,
        }
