"""Extracted data, its versions and what approving it changed.

Specification section 2.3 steps 4-7 and section 5.2. Extractions are versioned
and never edited in place: a reviewer's correction produces version N+1 with
``metodo='manual'`` and a diff against the machine output, so the record of who
confirmed what survives intact.
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
    ApplicationOutcome,
    ExtractionMethod,
    ExtractionState,
)


class Extraction(BaseModel):
    """One version of the structured data pulled out of a document."""

    __tablename__ = 'extractions'
    __table_args__ = (
        UniqueConstraint('document_id', 'version', name='uq_extractions_document_id_version'),
        db.Index('ix_extractions_document_estado', 'document_id', 'estado'),
        enum_check('estado', ExtractionState, 'extractions'),
        enum_check('metodo', ExtractionMethod, 'extractions'),
    )

    document_id = db.Column(
        GUID(), db.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False, index=True
    )
    version = db.Column(db.Integer, nullable=False, default=1)
    #: Exactly one version per document carries this flag.
    is_current = db.Column(db.Boolean, nullable=False, default=True, index=True)

    estado = enum_column(
        ExtractionState, nullable=False, default=ExtractionState.PENDIENTE, index=True
    )
    metodo = enum_column(ExtractionMethod, nullable=False, default=ExtractionMethod.IA)

    # --- Payload -------------------------------------------------------
    #: Raw model output, as returned.
    payload = db.Column(JSONBType())
    #: After dates, timezones, airport codes, locators and names are normalised.
    payload_normalizado = db.Column(JSONBType())
    #: Per-field confidence, keyed by the same field names as the payload.
    confianzas = db.Column(JSONBType())
    confianza_global = db.Column(db.Numeric(3, 2), index=True)
    #: Normalisation warnings: fields that could not be resolved.
    avisos = db.Column(JSONBType())
    schema_version = db.Column(db.String(20))

    # --- Provenance of the extraction itself ---------------------------
    proveedor = db.Column(db.String(40))
    modelo = db.Column(db.String(120))
    ai_run_id = db.Column(GUID(), db.ForeignKey('ai_runs.id'), nullable=True)

    # --- Review --------------------------------------------------------
    revisado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    revisado_en = db.Column(db.DateTime(timezone=True))
    aprobado_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    aprobado_en = db.Column(db.DateTime(timezone=True))
    comentario_revision = db.Column(db.Text)
    #: Field-level diff against the version this one corrects.
    diff_previa = db.Column(JSONBType())
    version_previa_id = db.Column(GUID(), db.ForeignKey('extractions.id'), nullable=True)

    # --- Relationships -------------------------------------------------
    document = db.relationship('Document', back_populates='extractions')
    revisado_por = db.relationship('User', foreign_keys=[revisado_por_id], lazy='joined')
    aprobado_por = db.relationship('User', foreign_keys=[aprobado_por_id], lazy='joined')
    ai_run = db.relationship('AIRun', lazy='select')
    version_previa = db.relationship('Extraction', remote_side='Extraction.id', lazy='select')
    applications = db.relationship(
        'ExtractionApplication', back_populates='extraction',
        cascade='all, delete-orphan', lazy='select',
    )

    def __repr__(self):
        return f'<Extraction {self.document_id} v{self.version} {self.estado}>'

    @property
    def datos(self):
        """The payload a reviewer should see: normalised when available."""
        return self.payload_normalizado or self.payload or {}

    @property
    def requiere_confirmacion(self):
        """True when confidence is too low to trust without a human.

        Specification section 2.3: "Ningún dato de extracción se considerará
        definitivo sin confirmación del gestor cuando la confianza sea baja o
        haya conflicto."
        """
        if self.avisos:
            return True
        if self.confianza_global is None:
            return True
        return float(self.confianza_global) < 0.85

    def confianza_de(self, campo):
        """Per-field confidence, or None when not reported."""
        if not self.confianzas:
            return None
        value = self.confianzas.get(campo)
        return float(value) if isinstance(value, (int, float)) else None

    def to_dict(self, include_payload=True):
        data = {
            'id': str(self.id),
            'document_id': str(self.document_id),
            'version': self.version,
            'is_current': self.is_current,
            'estado': str(self.estado),
            'estado_label': self.estado.label if self.estado else None,
            'metodo': str(self.metodo),
            'confianza_global': (
                float(self.confianza_global) if self.confianza_global is not None else None
            ),
            'requiere_confirmacion': self.requiere_confirmacion,
            'proveedor': self.proveedor,
            'modelo': self.modelo,
            'avisos': self.avisos or [],
            'revisado_por': self.revisado_por.to_reference() if self.revisado_por else None,
            'aprobado_por': self.aprobado_por.to_reference() if self.aprobado_por else None,
            'aprobado_en': self.aprobado_en.isoformat() if self.aprobado_en else None,
            'comentario_revision': self.comentario_revision,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
        if include_payload:
            data['datos'] = self.datos
            data['confianzas'] = self.confianzas or {}
        return data


class ExtractionApplication(BaseModel):
    """What approving an extraction did to one itinerary entity.

    Answers "which segment came from which document" without guessing, which is
    what the acceptance criterion of section 5.2 requires.
    """

    __tablename__ = 'extraction_applications'
    __table_args__ = (
        db.Index('ix_extraction_applications_entidad', 'entidad_tipo', 'entidad_id'),
        enum_check('resultado', ApplicationOutcome, 'extraction_applications'),
    )

    extraction_id = db.Column(
        GUID(), db.ForeignKey('extractions.id', ondelete='CASCADE'), nullable=False, index=True
    )
    entidad_tipo = db.Column(db.String(50), nullable=False)
    entidad_id = db.Column(GUID(), nullable=True)
    resultado = enum_column(ApplicationOutcome, nullable=False)
    campos_aplicados = db.Column(JSONBType())
    motivo = db.Column(db.String(400))

    extraction = db.relationship('Extraction', back_populates='applications')

    def __repr__(self):
        return f'<ExtractionApplication {self.entidad_tipo} {self.resultado}>'

    def to_dict(self):
        return {
            'id': str(self.id),
            'entidad_tipo': self.entidad_tipo,
            'entidad_id': str(self.entidad_id) if self.entidad_id else None,
            'resultado': str(self.resultado),
            'campos_aplicados': self.campos_aplicados or [],
            'motivo': self.motivo,
        }
