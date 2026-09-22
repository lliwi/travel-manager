"""In-app notifications (specification section 9, phase 2).

A notification is a thing the system already knows and the person does not yet:
an alert that just appeared on their trip, a source that changed under an
advisory they read last week, a document whose processing failed. Without
somewhere to put these, the only way to find out is to go and look, which means
they are found by whoever happens to look rather than by whoever needs to know.

Delivery by mail is a separate decision and a separate switch. What lands here
lands whether or not mail is configured, because the record of «we told them»
should not depend on a server being reachable.
"""
from app.extensions import db
from app.models.base import (
    GUID,
    BaseModel,
    JSONBType,
    enum_check,
    enum_column,
)
from app.models.enums import NotificationKind


class Notification(BaseModel):
    """Something one person should know about."""

    __tablename__ = 'notifications'
    __table_args__ = (
        db.Index('ix_notifications_usuario_leida', 'usuario_id', 'leida_en'),
        # One notification per fact per person: the alert engine reconciles and
        # the source watcher runs daily, so the same fact arrives again and
        # again. Without this, a trip with one open alert would grow a new
        # notification every night until nobody read any of them.
        db.UniqueConstraint('usuario_id', 'clave', name='uq_notifications_usuario_clave'),
        enum_check('tipo', NotificationKind, 'notifications'),
    )

    usuario_id = db.Column(
        GUID(), db.ForeignKey('users.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    tipo = enum_column(NotificationKind, nullable=False, index=True)

    #: Deterministic identity of the fact being reported, not of the message.
    clave = db.Column(db.String(200), nullable=False)

    titulo = db.Column(db.String(300), nullable=False)
    mensaje = db.Column(db.Text)
    #: Where to go to act on it.
    enlace = db.Column(db.String(500))

    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=True, index=True,
    )
    datos = db.Column(JSONBType())

    leida_en = db.Column(db.DateTime(timezone=True))
    enviada_por_correo_en = db.Column(db.DateTime(timezone=True))

    usuario = db.relationship('User', lazy='joined')
    trip = db.relationship('Trip', lazy='select')

    def __repr__(self):
        return f'<Notification {self.tipo} {self.usuario_id}>'

    @property
    def leida(self):
        return self.leida_en is not None

    def to_dict(self):
        return {
            'id': str(self.id),
            'tipo': str(self.tipo),
            'tipo_label': self.tipo.label if self.tipo else None,
            'titulo': self.titulo,
            'mensaje': self.mensaje,
            'enlace': self.enlace,
            'trip_id': str(self.trip_id) if self.trip_id else None,
            'leida': self.leida,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
