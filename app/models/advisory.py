"""Destination security advisories and the web sources they come from.

Specification sections 2.6 and 2.7. Every advisory must carry its source, the
source's own date, a confidence level and the disclaimer that it does not replace
official guidance -- and in phase 1 a manager validates it before travellers see
it.
"""
from sqlalchemy import UniqueConstraint

from app.extensions import db
from app.models.base import (
    GUID,
    BaseModel,
    JSONBType,
    SoftDeleteMixin,
    enum_check,
    enum_column,
)
from app.models.enums import (
    AdvisoryCategory,
    AdvisoryLevel,
    AdvisoryValidationState,
)

#: Shown with every advisory. Section 2.7 requires the warning explicitly.
DISCLAIMER = (
    'Esta información es orientativa y no sustituye a las fuentes oficiales. '
    'Consulte siempre los avisos del Ministerio de Asuntos Exteriores y de los '
    'organismos sanitarios competentes antes de viajar.'
)


class SecurityAdvisory(SoftDeleteMixin, BaseModel):
    """A recommendation tied to a destination and a date range."""

    __tablename__ = 'security_advisories'
    __table_args__ = (
        db.Index('ix_security_advisories_trip_estado', 'trip_id', 'estado_validacion'),
        db.Index('ix_security_advisories_pais_categoria', 'pais_codigo', 'categoria'),
        enum_check('nivel', AdvisoryLevel, 'security_advisories'),
        enum_check('categoria', AdvisoryCategory, 'security_advisories'),
        enum_check('estado_validacion', AdvisoryValidationState, 'security_advisories'),
    )

    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True
    )
    trip_destination_id = db.Column(
        GUID(), db.ForeignKey('trip_destinations.id', ondelete='SET NULL'), nullable=True
    )

    # --- Scope ---------------------------------------------------------
    pais_codigo = db.Column(db.String(2), index=True)
    ciudad = db.Column(db.String(160))
    categoria = enum_column(AdvisoryCategory, nullable=False, default=AdvisoryCategory.SEGURIDAD)
    nivel = enum_column(AdvisoryLevel, nullable=False, default=AdvisoryLevel.NORMAL, index=True)

    #: The travel window this advisory was generated for.
    aplica_desde = db.Column(db.DateTime(timezone=True))
    aplica_hasta = db.Column(db.DateTime(timezone=True))

    # --- Content -------------------------------------------------------
    titulo = db.Column(db.String(300), nullable=False)
    contenido = db.Column(db.Text, nullable=False)
    recomendaciones = db.Column(JSONBType())

    # --- Sourcing (section 2.7: source, date, confidence) --------------
    fuente = db.Column(db.String(250))
    fuente_url = db.Column(db.String(1000))
    #: The date the *source* published or last updated the information, which is
    #: not the same as when we fetched it.
    fecha_fuente = db.Column(db.DateTime(timezone=True))
    consultado_en = db.Column(db.DateTime(timezone=True))
    confianza = db.Column(db.Numeric(3, 2))
    #: Every source consulted, each with its url, title and fetch date.
    fuentes = db.Column(JSONBType())

    #: When this advisory should be regenerated.
    caduca_en = db.Column(db.DateTime(timezone=True), index=True)

    # --- Validation (section 2.7: a manager validates before publication)
    estado_validacion = enum_column(
        AdvisoryValidationState,
        nullable=False,
        default=AdvisoryValidationState.BORRADOR,
        index=True,
    )
    validado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    validado_en = db.Column(db.DateTime(timezone=True))
    comentario_validacion = db.Column(db.Text)

    # --- Generation ----------------------------------------------------
    generado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    ai_run_id = db.Column(GUID(), db.ForeignKey('ai_runs.id'), nullable=True)

    trip = db.relationship('Trip', back_populates='advisories')
    destino = db.relationship('TripDestination', lazy='select')
    validado_por = db.relationship('User', foreign_keys=[validado_por_id], lazy='joined')
    generado_por = db.relationship('User', foreign_keys=[generado_por_id], lazy='select')
    ai_run = db.relationship('AIRun', lazy='select')

    def __repr__(self):
        return f'<SecurityAdvisory {self.pais_codigo} {self.nivel} {self.estado_validacion}>'

    @property
    def is_published(self):
        """True when travellers are allowed to see it."""
        return self.estado_validacion is AdvisoryValidationState.VALIDADA

    @property
    def is_expired(self):
        from app.utils.timeutil import utcnow

        return self.caduca_en is not None and self.caduca_en < utcnow()

    def to_dict(self, include_sources=True):
        data = {
            'id': str(self.id),
            'trip_id': str(self.trip_id),
            'pais_codigo': self.pais_codigo,
            'ciudad': self.ciudad,
            'categoria': str(self.categoria),
            'categoria_label': self.categoria.label if self.categoria else None,
            'nivel': str(self.nivel),
            'nivel_label': self.nivel.label if self.nivel else None,
            'titulo': self.titulo,
            'contenido': self.contenido,
            'recomendaciones': self.recomendaciones or [],
            'fuente': self.fuente,
            'fuente_url': self.fuente_url,
            'fecha_fuente': self.fecha_fuente.isoformat() if self.fecha_fuente else None,
            'consultado_en': self.consultado_en.isoformat() if self.consultado_en else None,
            'confianza': float(self.confianza) if self.confianza is not None else None,
            'estado_validacion': str(self.estado_validacion),
            'validado_por': self.validado_por.to_reference() if self.validado_por else None,
            'validado_en': self.validado_en.isoformat() if self.validado_en else None,
            'caduca_en': self.caduca_en.isoformat() if self.caduca_en else None,
            'caducada': self.is_expired,
            'disclaimer': DISCLAIMER,
        }
        if include_sources:
            data['fuentes'] = self.fuentes or []
        return data


class WebSource(BaseModel):
    """An allow-listed domain the research service may consult.

    Specification section 2.6 requires configured, permitted sources; section 3.2
    requires SSRF protection. Nothing outside this table is ever fetched.
    """

    __tablename__ = 'web_sources'
    __table_args__ = (
        UniqueConstraint('dominio', name='uq_web_sources_dominio'),
        enum_check('categoria', AdvisoryCategory, 'web_sources'),
    )

    nombre = db.Column(db.String(250), nullable=False)
    dominio = db.Column(db.String(253), nullable=False, index=True)
    url_base = db.Column(db.String(1000))
    categoria = enum_column(AdvisoryCategory, nullable=False, default=AdvisoryCategory.SEGURIDAD)
    #: Official government or health-body sources outrank commercial ones.
    es_oficial = db.Column(db.Boolean, nullable=False, default=False, index=True)
    pais_codigo = db.Column(db.String(2))
    idioma = db.Column(db.String(10))
    activa = db.Column(db.Boolean, nullable=False, default=True, index=True)
    #: Ranking hint when several sources cover the same question.
    prioridad = db.Column(db.Integer, nullable=False, default=100)
    confianza_base = db.Column(db.Numeric(3, 2), default=0.7)
    notas = db.Column(db.Text)

    def __repr__(self):
        return f'<WebSource {self.dominio}>'

    def to_dict(self):
        return {
            'id': str(self.id),
            'nombre': self.nombre,
            'dominio': self.dominio,
            'url_base': self.url_base,
            'categoria': str(self.categoria),
            'es_oficial': self.es_oficial,
            'pais_codigo': self.pais_codigo,
            'idioma': self.idioma,
            'activa': self.activa,
            'prioridad': self.prioridad,
            'confianza_base': float(self.confianza_base) if self.confianza_base else None,
        }


class WebFetch(BaseModel):
    """Cached result of fetching one allow-listed URL.

    Section 2.6 requires showing links, the consultation date, the source and a
    summary; caching also keeps a burst of research from hammering an official
    site.
    """

    __tablename__ = 'web_fetches'
    __table_args__ = (
        db.Index('ix_web_fetches_url_hash_created', 'url_hash', 'created_at'),
    )

    web_source_id = db.Column(GUID(), db.ForeignKey('web_sources.id'), nullable=True, index=True)
    url = db.Column(db.String(2000), nullable=False)
    #: sha256 of the normalised URL, so the long URL need not be indexed.
    url_hash = db.Column(db.String(64), nullable=False, index=True)
    titulo = db.Column(db.String(500))
    #: Sanitised text, never raw HTML: the research content is untrusted input.
    contenido = db.Column(db.Text)
    resumen = db.Column(db.Text)
    status_code = db.Column(db.Integer)
    content_type = db.Column(db.String(120))
    bytes_descargados = db.Column(db.Integer)
    error = db.Column(db.String(500))
    expira_en = db.Column(db.DateTime(timezone=True), index=True)

    source = db.relationship('WebSource', lazy='joined')

    def __repr__(self):
        return f'<WebFetch {self.url[:60]}>'

    @property
    def is_fresh(self):
        from app.utils.timeutil import utcnow

        return self.expira_en is not None and self.expira_en > utcnow()

    def to_dict(self):
        return {
            'id': str(self.id),
            'url': self.url,
            'titulo': self.titulo,
            'resumen': self.resumen,
            'fuente': self.source.nombre if self.source else None,
            'es_oficial': self.source.es_oficial if self.source else False,
            'consultado_en': self.created_at.isoformat() if self.created_at else None,
        }
