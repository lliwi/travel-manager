"""AI provider configuration, per-task bindings and the execution log.

Specification section 2.5. Three things are non-negotiable here: API keys are
encrypted at rest and never leave the server, provider and model are selectable
per environment and per task, and every execution is logged with its purpose,
sources and metrics but *not* its full sensitive content.
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
    EXTERNAL_AI_PROVIDERS,
    AIProviderCode,
    AIRunState,
    AITask,
)


class AIProviderConfig(BaseModel):
    """One configured inference endpoint."""

    __tablename__ = 'ai_provider_configs'
    __table_args__ = (
        UniqueConstraint('nombre', name='uq_ai_provider_configs_nombre'),
        enum_check('proveedor', AIProviderCode, 'ai_provider_configs'),
    )

    nombre = db.Column(db.String(120), nullable=False)
    proveedor = enum_column(AIProviderCode, nullable=False, index=True)
    base_url = db.Column(db.String(500))
    modelo_por_defecto = db.Column(db.String(160))

    #: Fernet-encrypted. Never rendered, never logged, never put in a prompt.
    api_key_encrypted = db.Column(db.Text)
    #: Last four characters, for the administrator to recognise which key is set.
    api_key_pista = db.Column(db.String(8))

    activo = db.Column(db.Boolean, nullable=False, default=True, index=True)
    #: The provider used for any task without its own binding. Exactly one row
    #: carries it; ``set_default`` in the service keeps that true.
    es_por_defecto = db.Column(db.Boolean, nullable=False, default=False, index=True)
    timeout_segundos = db.Column(db.Integer, default=120)
    max_tokens = db.Column(db.Integer, default=2048)
    temperatura = db.Column(db.Numeric(3, 2), default=0.1)
    parametros = db.Column(JSONBType())

    # --- Health --------------------------------------------------------
    ultimo_chequeo_en = db.Column(db.DateTime(timezone=True))
    ultimo_chequeo_ok = db.Column(db.Boolean)
    ultimo_chequeo_detalle = db.Column(db.String(500))

    creado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    def __repr__(self):
        return f'<AIProviderConfig {self.nombre} ({self.proveedor})>'

    @property
    def es_externo(self):
        """True when using this provider sends data outside the perimeter.

        The egress policy of section 2.5 keys off this.
        """
        return self.proveedor in EXTERNAL_AI_PROVIDERS

    def to_dict(self):
        """Administrative representation. Deliberately omits the key itself."""
        return {
            'id': str(self.id),
            'nombre': self.nombre,
            'proveedor': str(self.proveedor),
            'proveedor_label': self.proveedor.label if self.proveedor else None,
            'base_url': self.base_url,
            'modelo_por_defecto': self.modelo_por_defecto,
            'tiene_api_key': bool(self.api_key_encrypted),
            'api_key_pista': self.api_key_pista,
            'activo': self.activo,
            'es_por_defecto': self.es_por_defecto,
            'es_externo': self.es_externo,
            'timeout_segundos': self.timeout_segundos,
            'max_tokens': self.max_tokens,
            'temperatura': float(self.temperatura) if self.temperatura is not None else None,
            'ultimo_chequeo_en': (
                self.ultimo_chequeo_en.isoformat() if self.ultimo_chequeo_en else None
            ),
            'ultimo_chequeo_ok': self.ultimo_chequeo_ok,
            'ultimo_chequeo_detalle': self.ultimo_chequeo_detalle,
        }


class AITaskBinding(BaseModel):
    """Which provider and model serve one task (section 2.5: selection by task)."""

    __tablename__ = 'ai_task_bindings'
    __table_args__ = (
        UniqueConstraint('tarea', name='uq_ai_task_bindings_tarea'),
        enum_check('tarea', AITask, 'ai_task_bindings'),
    )

    tarea = enum_column(AITask, nullable=False, index=True)
    provider_config_id = db.Column(
        GUID(), db.ForeignKey('ai_provider_configs.id'), nullable=False
    )
    modelo = db.Column(db.String(160))
    max_tokens = db.Column(db.Integer)
    temperatura = db.Column(db.Numeric(3, 2))
    activo = db.Column(db.Boolean, nullable=False, default=True)
    #: Fallback used when the primary provider is unreachable.
    fallback_config_id = db.Column(
        GUID(), db.ForeignKey('ai_provider_configs.id'), nullable=True
    )

    provider_config = db.relationship(
        'AIProviderConfig', foreign_keys=[provider_config_id], lazy='joined'
    )
    fallback_config = db.relationship(
        'AIProviderConfig', foreign_keys=[fallback_config_id], lazy='select'
    )

    def __repr__(self):
        return f'<AITaskBinding {self.tarea} -> {self.provider_config_id}>'

    def to_dict(self):
        return {
            'id': str(self.id),
            'tarea': str(self.tarea),
            'tarea_label': self.tarea.label if self.tarea else None,
            'proveedor': self.provider_config.nombre if self.provider_config else None,
            'modelo': self.modelo or (
                self.provider_config.modelo_por_defecto if self.provider_config else None
            ),
            'activo': self.activo,
            'fallback': self.fallback_config.nombre if self.fallback_config else None,
        }


class AIRun(BaseModel):
    """One AI execution (specification section 2.5, logging requirements).

    Records who asked, which provider and model answered, what for, which
    internal sources were authorised, and the metrics. It records a *summary* of
    the result, not the full sensitive content, unless an administrator has
    explicitly approved verbose capture.
    """

    __tablename__ = 'ai_runs'
    __table_args__ = (
        db.Index('ix_ai_runs_usuario_created', 'usuario_id', 'created_at'),
        db.Index('ix_ai_runs_trip_created', 'trip_id', 'created_at'),
        enum_check('tarea', AITask, 'ai_runs'),
        enum_check('estado', AIRunState, 'ai_runs'),
        enum_check('proveedor', AIProviderCode, 'ai_runs'),
    )

    usuario_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True, index=True)
    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='SET NULL'), nullable=True, index=True
    )
    document_id = db.Column(
        GUID(), db.ForeignKey('documents.id', ondelete='SET NULL'), nullable=True
    )

    tarea = enum_column(AITask, nullable=False, index=True)
    proveedor = enum_column(AIProviderCode, nullable=False)
    modelo = db.Column(db.String(160))
    estado = enum_column(AIRunState, nullable=False, default=AIRunState.PENDIENTE, index=True)

    # --- Purpose and sources -------------------------------------------
    finalidad = db.Column(db.String(300))
    #: Ids of the internal records the model was allowed to see. The answer's
    #: references are post-validated against this set (section 5.4).
    referencias_fuentes = db.Column(JSONBType())
    #: Public URLs consulted, when the task involved web research.
    fuentes_web = db.Column(JSONBType())

    # --- Result --------------------------------------------------------
    #: A short summary, not the full output.
    resultado_resumen = db.Column(db.Text)
    #: Populated when the egress policy refused, or the provider failed.
    motivo_bloqueo = db.Column(db.String(500))
    error = db.Column(db.String(500))

    # --- Metrics -------------------------------------------------------
    tokens_entrada = db.Column(db.Integer)
    tokens_salida = db.Column(db.Integer)
    duracion_ms = db.Column(db.Integer)
    coste_estimado = db.Column(db.Numeric(10, 4))
    reintentos = db.Column(db.Integer, nullable=False, default=0)

    correlation_id = db.Column(db.String(64), index=True)

    usuario = db.relationship('User', foreign_keys=[usuario_id], lazy='joined')
    trip = db.relationship('Trip', foreign_keys=[trip_id], lazy='select')

    def __repr__(self):
        return f'<AIRun {self.tarea} {self.proveedor} {self.estado}>'

    def to_dict(self):
        return {
            'id': str(self.id),
            'usuario': self.usuario.to_reference() if self.usuario else None,
            'trip_id': str(self.trip_id) if self.trip_id else None,
            'tarea': str(self.tarea),
            'tarea_label': self.tarea.label if self.tarea else None,
            'proveedor': str(self.proveedor),
            'modelo': self.modelo,
            'estado': str(self.estado),
            'estado_label': self.estado.label if self.estado else None,
            'finalidad': self.finalidad,
            'referencias_fuentes': self.referencias_fuentes or {},
            'fuentes_web': self.fuentes_web or [],
            'resultado_resumen': self.resultado_resumen,
            'motivo_bloqueo': self.motivo_bloqueo,
            'error': self.error,
            'tokens_entrada': self.tokens_entrada,
            'tokens_salida': self.tokens_salida,
            'duracion_ms': self.duracion_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
