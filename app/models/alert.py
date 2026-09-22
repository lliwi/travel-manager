"""Alerts, their state history and the configurable rule thresholds.

Specification sections 2.4 and 5.3. The ``dedup_key`` is what lets the engine be
re-run any number of times without duplicating alerts or resurrecting ones a
manager has already dismissed.
"""
from sqlalchemy import UniqueConstraint

from app.extensions import db
from app.models.base import (
    GUID,
    BaseModel,
    JSONBType,
    enum_check,
    enum_column,
)
from app.models.enums import (
    AlertSeverity,
    AlertState,
    AlertTrigger,
)
from app.utils.timeutil import utcnow


class Alert(BaseModel):
    """A detected itinerary problem."""

    __tablename__ = 'alerts'
    __table_args__ = (
        # One live alert per (trip, rule, involved entities). Re-running the
        # engine updates this row instead of inserting a second one.
        UniqueConstraint('trip_id', 'dedup_key', name='uq_alerts_trip_id_dedup_key'),
        db.Index('ix_alerts_trip_estado_severidad', 'trip_id', 'estado', 'severidad'),
        db.Index('ix_alerts_traveler_estado', 'trip_traveler_id', 'estado'),
        enum_check('severidad', AlertSeverity, 'alerts'),
        enum_check('estado', AlertState, 'alerts'),
    )

    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True
    )
    #: Null for trip-wide alerts; set for per-traveller ones.
    trip_traveler_id = db.Column(
        GUID(), db.ForeignKey('trip_travelers.id', ondelete='CASCADE'), nullable=True, index=True
    )

    # --- Identity ------------------------------------------------------
    #: The rule that produced this alert, e.g. ``conexion_margen_insuficiente``.
    tipo = db.Column(db.String(80), nullable=False, index=True)
    regla = db.Column(db.String(120), nullable=False)
    #: sha256 over rule code, trip, traveller and the sorted involved entity ids.
    dedup_key = db.Column(db.String(64), nullable=False, index=True)

    severidad = enum_column(AlertSeverity, nullable=False, index=True)
    estado = enum_column(AlertState, nullable=False, default=AlertState.ABIERTA, index=True)

    # --- Content -------------------------------------------------------
    titulo = db.Column(db.String(300), nullable=False)
    mensaje = db.Column(db.Text, nullable=False)
    #: Everything needed to explain the alert: involved entity ids, the computed
    #: margin, the threshold applied and why that threshold was chosen.
    evidencia = db.Column(JSONBType())
    sugerencia = db.Column(db.Text)

    # --- Lifecycle -----------------------------------------------------
    generada_en = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    #: Refreshed each time the engine still sees the condition.
    vista_por_ultima_vez_en = db.Column(db.DateTime(timezone=True), default=utcnow)
    resuelta_en = db.Column(db.DateTime(timezone=True))
    resuelta_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    responsable_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True, index=True)
    comentario = db.Column(db.Text)
    #: True when the engine closed it because the condition no longer holds.
    cierre_automatico = db.Column(db.Boolean, nullable=False, default=False)

    # --- Relationships -------------------------------------------------
    trip = db.relationship('Trip', back_populates='alerts')
    traveler = db.relationship('TripTraveler', lazy='joined')
    resuelta_por = db.relationship('User', foreign_keys=[resuelta_por_id], lazy='joined')
    responsable = db.relationship('User', foreign_keys=[responsable_id], lazy='joined')
    transitions = db.relationship(
        'AlertTransition', back_populates='alert', cascade='all, delete-orphan',
        order_by='AlertTransition.created_at', lazy='select',
    )

    def __repr__(self):
        return f'<Alert {self.tipo} {self.severidad} {self.estado}>'

    @property
    def is_open(self):
        return self.estado is AlertState.ABIERTA

    @property
    def is_acknowledged(self):
        """True once a manager has accepted or dismissed it.

        The reconciler must not reopen these while the dedup key is unchanged.
        """
        return self.estado in (AlertState.ACEPTADA, AlertState.DESCARTADA)

    def to_dict(self, include_evidence=True, include_transitions=False):
        data = {
            'id': str(self.id),
            'trip_id': str(self.trip_id),
            'trip_traveler_id': str(self.trip_traveler_id) if self.trip_traveler_id else None,
            'tipo': self.tipo,
            'regla': self.regla,
            'severidad': str(self.severidad),
            'severidad_label': self.severidad.label if self.severidad else None,
            'estado': str(self.estado),
            'estado_label': self.estado.label if self.estado else None,
            'titulo': self.titulo,
            'mensaje': self.mensaje,
            'sugerencia': self.sugerencia,
            'generada_en': self.generada_en.isoformat() if self.generada_en else None,
            'resuelta_en': self.resuelta_en.isoformat() if self.resuelta_en else None,
            'resuelta_por': self.resuelta_por.to_reference() if self.resuelta_por else None,
            'responsable': self.responsable.to_reference() if self.responsable else None,
            'comentario': self.comentario,
            'cierre_automatico': self.cierre_automatico,
        }
        if include_evidence:
            data['evidencia'] = self.evidencia or {}
        if include_transitions:
            data['transiciones'] = [t.to_dict() for t in self.transitions]
        return data


class AlertTransition(BaseModel):
    """Every state change of an alert, with the actor's comment.

    Section 5.3 requires accepting or resolving an alert *with a comment*; this
    is where that comment is preserved even if the alert is later reopened.
    """

    __tablename__ = 'alert_transitions'
    __table_args__ = (
        db.Index('ix_alert_transitions_alert_created', 'alert_id', 'created_at'),
        enum_check('estado_nuevo', AlertState, 'alert_transitions'),
    )

    alert_id = db.Column(
        GUID(), db.ForeignKey('alerts.id', ondelete='CASCADE'), nullable=False, index=True
    )
    estado_anterior = enum_column(AlertState, nullable=True)
    estado_nuevo = enum_column(AlertState, nullable=False)
    comentario = db.Column(db.Text)
    actor_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    #: True when the engine changed the state rather than a person.
    automatico = db.Column(db.Boolean, nullable=False, default=False)

    alert = db.relationship('Alert', back_populates='transitions')
    actor = db.relationship('User', foreign_keys=[actor_id], lazy='joined')

    def to_dict(self):
        return {
            'estado_anterior': str(self.estado_anterior) if self.estado_anterior else None,
            'estado_nuevo': str(self.estado_nuevo),
            'comentario': self.comentario,
            'actor': self.actor.to_reference() if self.actor else None,
            'automatico': self.automatico,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class AlertRuleSetting(BaseModel):
    """Administrator-configurable behaviour of one rule.

    Section 2.4 requires the thresholds to be configurable, giving 90 minutes
    for Schengen connections and 150 for international ones as the example.
    """

    __tablename__ = 'alert_rule_settings'
    __table_args__ = (
        UniqueConstraint('regla', name='uq_alert_rule_settings_regla'),
        enum_check('severidad', AlertSeverity, 'alert_rule_settings'),
    )

    regla = db.Column(db.String(120), nullable=False, index=True)
    nombre = db.Column(db.String(200), nullable=False)
    descripcion = db.Column(db.Text)
    activa = db.Column(db.Boolean, nullable=False, default=True)
    severidad = enum_column(AlertSeverity, nullable=True)
    #: Rule-specific thresholds, e.g.
    #: ``{"schengen_min": 90, "internacional_min": 150}``.
    parametros = db.Column(JSONBType())
    actualizada_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    actualizada_por = db.relationship('User', foreign_keys=[actualizada_por_id], lazy='select')

    def __repr__(self):
        return f'<AlertRuleSetting {self.regla}>'

    def get(self, key, default=None):
        """Read one threshold, falling back to the rule's own default."""
        if not self.parametros:
            return default
        return self.parametros.get(key, default)

    def to_dict(self):
        return {
            'id': str(self.id),
            'regla': self.regla,
            'nombre': self.nombre,
            'descripcion': self.descripcion,
            'activa': self.activa,
            'severidad': str(self.severidad) if self.severidad else None,
            'parametros': self.parametros or {},
        }


class AlertRun(BaseModel):
    """One execution of the alert engine over one trip.

    Gives the operator a record of when alerts were last computed, what triggered
    it and what changed -- which matters when a manager asks why an alert
    appeared or vanished.
    """

    __tablename__ = 'alert_runs'
    __table_args__ = (
        db.Index('ix_alert_runs_trip_created', 'trip_id', 'created_at'),
        enum_check('disparador', AlertTrigger, 'alert_runs'),
    )

    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True
    )
    disparador = enum_column(AlertTrigger, nullable=False, default=AlertTrigger.MANUAL)
    itinerary_version = db.Column(db.Integer)

    reglas_evaluadas = db.Column(db.Integer, nullable=False, default=0)
    creadas = db.Column(db.Integer, nullable=False, default=0)
    actualizadas = db.Column(db.Integer, nullable=False, default=0)
    cerradas = db.Column(db.Integer, nullable=False, default=0)
    sin_cambios = db.Column(db.Integer, nullable=False, default=0)

    duracion_ms = db.Column(db.Integer)
    error = db.Column(db.String(500))
    actor_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    trip = db.relationship('Trip', lazy='select')
    actor = db.relationship('User', foreign_keys=[actor_id], lazy='select')

    def __repr__(self):
        return f'<AlertRun {self.trip_id} +{self.creadas}/~{self.actualizadas}/-{self.cerradas}>'

    def to_dict(self):
        return {
            'id': str(self.id),
            'disparador': str(self.disparador),
            'reglas_evaluadas': self.reglas_evaluadas,
            'creadas': self.creadas,
            'actualizadas': self.actualizadas,
            'cerradas': self.cerradas,
            'sin_cambios': self.sin_cambios,
            'duracion_ms': self.duracion_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
