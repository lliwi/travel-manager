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
    AIAutotuneState,
    AIParameterOrigin,
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
    #: Ask the model not to reason before answering, where it can be asked.
    #: A reasoning model spends its output budget thinking, and with a tight
    #: limit there is nothing left to answer with: the call succeeds, empty.
    sin_razonamiento = db.Column(db.Boolean(), nullable=False, default=False)

    #: What this endpoint has already told us it will not accept. Not a
    #: decision anybody makes -- it is discovered, once, from a refusal.
    parametros = db.Column(JSONBType())
    #: Sampling parameters for every model and task on this endpoint, the
    #: lowest layer. Keys from ``ai/parametros.CATALOGO``; absent means the
    #: endpoint's own default.
    parametros_avanzados = db.Column(JSONBType())

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
            'parametros_avanzados': self.parametros_avanzados or {},
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


class AIParameterProfile(BaseModel):
    """Sampling parameters for one model on one provider, for one task or all.

    ``tarea`` NULL is the model's profile for every task; a row with a task is
    the one layered last, over what the task asks for in code. See
    ``ai/parametros.py`` for the order and why.
    """

    __tablename__ = 'ai_parameter_profiles'
    __table_args__ = (
        # NULLS NOT DISTINCT: otherwise PostgreSQL would accept any number of
        # «every task» rows for the same model, and which one applied would
        # depend on the order the rows came back in.
        UniqueConstraint(
            'provider_config_id', 'modelo', 'tarea',
            name='uq_ai_parameter_profiles_alcance',
            postgresql_nulls_not_distinct=True,
        ),
        enum_check('tarea', AITask, 'ai_parameter_profiles'),
        enum_check('origen', AIParameterOrigin, 'ai_parameter_profiles'),
    )

    provider_config_id = db.Column(
        GUID(), db.ForeignKey('ai_provider_configs.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    modelo = db.Column(db.String(160), nullable=False)
    tarea = enum_column(AITask, nullable=True)
    parametros = db.Column(JSONBType(), nullable=False, default=dict)
    origen = enum_column(AIParameterOrigin, nullable=False,
                         default=AIParameterOrigin.MANUAL)
    #: The tuning run that wrote it, when it was the tuner.
    autotune_run_id = db.Column(
        GUID(), db.ForeignKey('ai_autotune_runs.id', ondelete='SET NULL'),
        nullable=True,
    )
    actualizado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    provider_config = db.relationship('AIProviderConfig', lazy='joined')
    actualizado_por = db.relationship('User', lazy='select')

    def __repr__(self):
        return f'<AIParameterProfile {self.modelo} {self.tarea or "*"}>'

    @property
    def alcance_label(self):
        return self.tarea.label if self.tarea else 'Todas las tareas'

    def to_dict(self):
        return {
            'id': str(self.id),
            'proveedor': self.provider_config.nombre if self.provider_config else None,
            'modelo': self.modelo,
            'tarea': str(self.tarea) if self.tarea else None,
            'parametros': self.parametros or {},
            'origen': str(self.origen),
            'autotune_run_id': str(self.autotune_run_id) if self.autotune_run_id else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class AIAutotuneRun(BaseModel):
    """One search for better parameters for a task on a provider and model.

    Keeps every trial, not only the winner: «why did it pick this?» has to be
    answerable by reading the row, and a tuner whose choices cannot be
    reconstructed is one nobody should let change a production setting.
    """

    __tablename__ = 'ai_autotune_runs'
    __table_args__ = (
        enum_check('tarea', AITask, 'ai_autotune_runs'),
        enum_check('estado', AIAutotuneState, 'ai_autotune_runs'),
        db.Index('ix_ai_autotune_runs_tarea_created', 'tarea', 'created_at'),
    )

    tarea = enum_column(AITask, nullable=False)
    provider_config_id = db.Column(
        GUID(), db.ForeignKey('ai_provider_configs.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    modelo = db.Column(db.String(160), nullable=False)
    estado = enum_column(AIAutotuneState, nullable=False,
                         default=AIAutotuneState.PENDIENTE, index=True)

    # --- What was asked ----------------------------------------------
    #: ``[[clave, [valores]]]`` -- the grid actually searched, in order.
    espacio = db.Column(JSONBType(), nullable=False, default=list)
    presupuesto = db.Column(db.Integer, nullable=False, default=12)
    repeticiones = db.Column(db.Integer, nullable=False, default=1)
    aplicar_si_mejora = db.Column(db.Boolean, nullable=False, default=False)
    automatico = db.Column(db.Boolean, nullable=False, default=False)

    # --- What happened -----------------------------------------------
    casos = db.Column(db.Integer)
    #: ``[{parametros, calidad, duracion_ms, tokens, errores, detalle}]``.
    ensayos = db.Column(JSONBType(), nullable=False, default=list)
    parametros_base = db.Column(JSONBType())
    parametros_mejores = db.Column(JSONBType())
    calidad_base = db.Column(db.Numeric(5, 1))
    calidad_mejor = db.Column(db.Numeric(5, 1))
    error = db.Column(db.String(500))
    celery_task_id = db.Column(db.String(64))

    iniciado_en = db.Column(db.DateTime(timezone=True))
    terminado_en = db.Column(db.DateTime(timezone=True))

    # --- What was done with it ---------------------------------------
    aplicado_en = db.Column(db.DateTime(timezone=True))
    aplicado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    #: The task profile as it was before applying, so it can be put back.
    #: ``None`` with ``aplicado_en`` set means there was no profile.
    parametros_previos = db.Column(JSONBType())
    revertido_en = db.Column(db.DateTime(timezone=True))

    lanzado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)

    provider_config = db.relationship('AIProviderConfig', lazy='joined')
    lanzado_por = db.relationship('User', foreign_keys=[lanzado_por_id], lazy='select')
    aplicado_por = db.relationship('User', foreign_keys=[aplicado_por_id], lazy='select')

    def __repr__(self):
        return f'<AIAutotuneRun {self.tarea} {self.modelo} {self.estado}>'

    @property
    def activo(self):
        return self.estado in (AIAutotuneState.PENDIENTE, AIAutotuneState.EN_CURSO)

    @property
    def mejora(self):
        """Quality points gained over the baseline, or None before finishing."""
        if self.calidad_mejor is None or self.calidad_base is None:
            return None
        return float(self.calidad_mejor) - float(self.calidad_base)

    @property
    def hay_propuesta(self):
        """True when the search found something different from what was there."""
        return (
            self.estado is AIAutotuneState.COMPLETADO
            and self.parametros_mejores is not None
            and self.parametros_mejores != self.parametros_base
        )

    @property
    def aplicado(self):
        return self.aplicado_en is not None and self.revertido_en is None

    def to_dict(self):
        return {
            'id': str(self.id),
            'tarea': str(self.tarea),
            'tarea_label': self.tarea.label if self.tarea else None,
            'proveedor': self.provider_config.nombre if self.provider_config else None,
            'modelo': self.modelo,
            'estado': str(self.estado),
            'estado_label': self.estado.label if self.estado else None,
            'espacio': self.espacio or [],
            'presupuesto': self.presupuesto,
            'repeticiones': self.repeticiones,
            'casos': self.casos,
            'ensayos': self.ensayos or [],
            'parametros_base': self.parametros_base,
            'parametros_mejores': self.parametros_mejores,
            'calidad_base': float(self.calidad_base) if self.calidad_base is not None else None,
            'calidad_mejor': float(self.calidad_mejor) if self.calidad_mejor is not None else None,
            'mejora': self.mejora,
            'aplicado': self.aplicado,
            'error': self.error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
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
    #: The sampling parameters actually sent, after every layer. Without them
    #: a run cannot be compared with another, nor a tuned setting traced to
    #: the answers it produced.
    parametros = db.Column(JSONBType())

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
            'parametros': self.parametros or {},
            'resultado_resumen': self.resultado_resumen,
            'motivo_bloqueo': self.motivo_bloqueo,
            'error': self.error,
            'tokens_entrada': self.tokens_entrada,
            'tokens_salida': self.tokens_salida,
            'duracion_ms': self.duracion_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
