"""Documents, their extracted text and their processing state history.

Specification section 2.3. The original file is never modified and never leaves
object storage; the database holds only metadata, the SHA-256 hash and the
object key (section 3.1).
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
    DOCUMENT_PIPELINE_ORDER,
    AntivirusStatus,
    DocumentClassification,
    DocumentProcessState,
    DocumentType,
    ProvenanceOrigin,
)


class Document(SoftDeleteMixin, BaseModel):
    """An uploaded file attached to a trip."""

    __tablename__ = 'documents'
    __table_args__ = (
        # The same file uploaded twice to the same trip is a duplicate. Partial
        # so that soft-deleted rows do not block a re-upload.
        db.Index(
            'uq_documents_trip_hash_activo',
            'trip_id', 'hash_sha256',
            unique=True,
            postgresql_where=db.text('is_deleted = false'),
            sqlite_where=db.text('is_deleted = 0'),
        ),
        db.Index('ix_documents_trip_estado', 'trip_id', 'estado_proceso'),
        enum_check('tipo', DocumentType, 'documents'),
        enum_check('estado_proceso', DocumentProcessState, 'documents'),
        enum_check('antivirus_estado', AntivirusStatus, 'documents'),
    )

    trip_id = db.Column(
        GUID(), db.ForeignKey('trips.id', ondelete='CASCADE'), nullable=False, index=True
    )
    #: Optional: a document that concerns one traveller only.
    trip_traveler_id = db.Column(
        GUID(), db.ForeignKey('trip_travelers.id'), nullable=True, index=True
    )

    # --- File identity -------------------------------------------------
    nombre_original = db.Column(db.String(400), nullable=False)
    #: Declared by the uploader; what they say the file is for.
    tipo = enum_column(DocumentType, nullable=False, default=DocumentType.OTRO)
    mime = db.Column(db.String(120))
    mime_declarado = db.Column(db.String(120))
    extension = db.Column(db.String(12))
    tamano_bytes = db.Column(db.BigInteger)
    paginas = db.Column(db.Integer)
    hash_sha256 = db.Column(db.String(64), nullable=False, index=True)

    # --- Storage (section 3.1: attachments live outside PostgreSQL) ----
    storage_backend = db.Column(db.String(30))
    bucket = db.Column(db.String(120))
    objeto_storage = db.Column(db.String(600))
    #: Temporary key while the file awaits validation and antivirus.
    objeto_cuarentena = db.Column(db.String(600))

    # --- Pipeline ------------------------------------------------------
    estado_proceso = enum_column(
        DocumentProcessState,
        nullable=False,
        default=DocumentProcessState.RECIBIDO,
        index=True,
    )
    celery_task_id = db.Column(db.String(64), index=True)
    intentos = db.Column(db.Integer, nullable=False, default=0)
    error_codigo = db.Column(db.String(60))
    error_mensaje = db.Column(db.String(500))

    # --- Antivirus -----------------------------------------------------
    antivirus_estado = enum_column(
        AntivirusStatus, nullable=False, default=AntivirusStatus.PENDIENTE
    )
    antivirus_motor = db.Column(db.String(120))
    antivirus_firma = db.Column(db.String(200))
    antivirus_at = db.Column(db.DateTime(timezone=True))

    # --- Classification (what the system detected) ---------------------
    clasificacion = enum_column(
        DocumentClassification,
        nullable=False,
        default=DocumentClassification.DESCONOCIDO,
        index=True,
    )
    clasificacion_confianza = db.Column(db.Numeric(3, 2))
    clasificacion_origen = enum_column(ProvenanceOrigin, nullable=True)

    # --- Text ----------------------------------------------------------
    tiene_texto_extraible = db.Column(db.Boolean)
    uso_ocr = db.Column(db.Boolean, nullable=False, default=False)
    idioma_detectado = db.Column(db.String(10))

    # --- Traceability --------------------------------------------------
    subido_por_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=False, index=True)
    subido_en = db.Column(db.DateTime(timezone=True))
    datos_validacion = db.Column(JSONBType())
    #: When the retention policy allows this original to be erased.
    retencion_hasta = db.Column(db.DateTime(timezone=True), index=True)

    # --- Relationships -------------------------------------------------
    trip = db.relationship('Trip', back_populates='documents')
    traveler = db.relationship('TripTraveler', lazy='select')
    subido_por = db.relationship('User', foreign_keys=[subido_por_id], lazy='joined')
    texto = db.relationship(
        'DocumentText', back_populates='document', uselist=False,
        cascade='all, delete-orphan', lazy='select',
    )
    pages = db.relationship(
        'DocumentPage', back_populates='document', cascade='all, delete-orphan',
        order_by='DocumentPage.numero', lazy='select',
    )
    extractions = db.relationship(
        'Extraction', back_populates='document', cascade='all, delete-orphan',
        order_by='Extraction.version', lazy='select',
    )
    transitions = db.relationship(
        'DocumentStateTransition', back_populates='document', cascade='all, delete-orphan',
        order_by='DocumentStateTransition.created_at', lazy='select',
    )

    def __repr__(self):
        return f'<Document {self.nombre_original} {self.estado_proceso}>'

    # ------------------------------------------------------------------
    # Pipeline helpers
    # ------------------------------------------------------------------
    @property
    def resumen_contenido(self):
        """What this document is about, in the words of what was extracted.

        The list showed file names, which are whatever the airline's mail
        server produced: «Your booking confirmation ODIVYR - BCN-LGW 31
        October, LGW-BCN 02 November.eml» wraps over two lines, buries the
        route in the middle and looks like every other one. What somebody
        scanning the list needs is which flight it is.

        Returns None when nothing has been extracted yet, and the caller falls
        back to the file name -- which is still better than an empty row.
        """
        extraccion = self.current_extraction
        servicios = ((extraccion.payload or {}).get('servicios')
                     if extraccion else None)
        if not servicios:
            return None

        etiquetas = [e for e in (_etiqueta_de_servicio(s) for s in servicios) if e]
        if not etiquetas:
            return None

        if len(etiquetas) <= 2:
            return ' · '.join(etiquetas)
        return f'{etiquetas[0]} · {etiquetas[1]} +{len(etiquetas) - 2}'

    def cubre_segmento(self, segmento):
        """Whether this document plausibly belongs to that itinerary segment.

        Used to decide whether a flight already has its boarding pass, so the
        direction of every doubt matters: failing to match one raises an alert
        for a flight somebody has already checked in for, which is a nuisance;
        matching the wrong one silences the alert for a flight nobody has
        checked in for, which is the thing worth catching.

        So the two signals are both narrow. Either the segment was built from
        this very document, or the document's own text names that flight
        number. A return flight is a different number, so one pass never
        answers for both legs.
        """
        if segmento is None:
            return False

        if getattr(segmento, 'documento_origen_id', None) == self.id:
            return True

        # A document assigned to one traveller says nothing about another's.
        if (self.trip_traveler_id is not None
                and segmento.trip_traveler_id is not None
                and self.trip_traveler_id != segmento.trip_traveler_id):
            return False

        numero = (getattr(segmento, 'numero', None) or '').replace(' ', '').upper()
        if not numero:
            return False

        texto = (self.texto.contenido if self.texto else '') or ''
        return numero in texto.replace(' ', '').upper()

    @property
    def current_extraction(self):
        """The extraction version currently under review or approved."""
        for extraction in reversed(self.extractions):
            if extraction.is_current:
                return extraction
        return None

    @property
    def progreso(self):
        """``(paso, total)`` for the progress indicator."""
        total = len(DOCUMENT_PIPELINE_ORDER)
        try:
            step = DOCUMENT_PIPELINE_ORDER.index(self.estado_proceso) + 1
        except ValueError:
            step = 0
        return step, total

    @property
    def tamano_legible(self):
        """Human-readable file size."""
        size = float(self.tamano_bytes or 0)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024 or unit == 'GB':
                return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} GB'

    @property
    def puede_reanalizarse(self):
        """Whether reading this document again would do anything.

        A reprocess rewinds to classification, so it needs a document that got
        at least that far and is not in a terminal state. Offering it on one
        that never got there would send it to a task that can only fail, and
        offering it on a rejected one would promise something that cannot
        happen.
        """
        from app.models.enums import DOCUMENT_TERMINAL_STATES, DocumentProcessState

        if self.estado_proceso in DOCUMENT_TERMINAL_STATES:
            return False
        return self.estado_proceso in (
            DocumentProcessState.CLASIFICADO,
            DocumentProcessState.EXTRAIDO,
            DocumentProcessState.NORMALIZADO,
            DocumentProcessState.PENDIENTE_REVISION,
            DocumentProcessState.REVISADO,
            DocumentProcessState.APROBADO,
        )

    @property
    def esta_disponible(self):
        """True when the original can still be downloaded."""
        return (
            not self.is_deleted
            and self.objeto_storage is not None
            and self.estado_proceso is not DocumentProcessState.INFECTADO
        )

    def to_dict(self, include_transitions=False):
        step, total = self.progreso
        data = {
            'id': str(self.id),
            'trip_id': str(self.trip_id),
            'nombre_original': self.nombre_original,
            'tipo': str(self.tipo),
            'tipo_label': self.tipo.label if self.tipo else None,
            'mime': self.mime,
            'extension': self.extension,
            'tamano_bytes': self.tamano_bytes,
            'tamano_legible': self.tamano_legible,
            'paginas': self.paginas,
            'hash_sha256': self.hash_sha256,
            'estado_proceso': str(self.estado_proceso),
            'estado_label': self.estado_proceso.label if self.estado_proceso else None,
            'progreso': {'paso': step, 'total': total},
            'clasificacion': str(self.clasificacion),
            'clasificacion_label': self.clasificacion.label if self.clasificacion else None,
            'clasificacion_confianza': (
                float(self.clasificacion_confianza)
                if self.clasificacion_confianza is not None else None
            ),
            'antivirus_estado': str(self.antivirus_estado),
            'uso_ocr': self.uso_ocr,
            'idioma_detectado': self.idioma_detectado,
            'subido_por': self.subido_por.to_reference() if self.subido_por else None,
            'subido_en': self.subido_en.isoformat() if self.subido_en else None,
            'disponible': self.esta_disponible,
        }
        if self.error_codigo:
            data['error'] = {'codigo': self.error_codigo, 'mensaje': self.error_mensaje}
        if include_transitions:
            data['transiciones'] = [t.to_dict() for t in self.transitions]
        return data


class DocumentText(BaseModel):
    """Full extracted text of a document, kept separate so it is not loaded
    with every document listing."""

    __tablename__ = 'document_texts'

    document_id = db.Column(
        GUID(), db.ForeignKey('documents.id', ondelete='CASCADE'),
        nullable=False, unique=True, index=True,
    )
    contenido = db.Column(db.Text)
    caracteres = db.Column(db.Integer)
    motor = db.Column(db.String(60))

    document = db.relationship('Document', back_populates='texto')

    def __repr__(self):
        return f'<DocumentText {self.document_id} ({self.caracteres} chars)>'


class DocumentPage(BaseModel):
    """Per-page text, so extracted values can cite the page they came from.

    Specification section 2.3: extracted data must reference "el documento/página
    de referencia cuando sea posible".
    """

    __tablename__ = 'document_pages'
    __table_args__ = (
        UniqueConstraint('document_id', 'numero', name='uq_document_pages_document_id_numero'),
    )

    document_id = db.Column(
        GUID(), db.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False, index=True
    )
    numero = db.Column(db.Integer, nullable=False)
    contenido = db.Column(db.Text)
    caracteres = db.Column(db.Integer)
    uso_ocr = db.Column(db.Boolean, nullable=False, default=False)
    confianza_ocr = db.Column(db.Numeric(3, 2))

    document = db.relationship('Document', back_populates='pages')

    def __repr__(self):
        return f'<DocumentPage {self.document_id} p{self.numero}>'


class DocumentStateTransition(BaseModel):
    """Every move through the document state machine.

    Written exclusively by ``document_service.transition``; this table is what
    makes a failed pipeline diagnosable and a partial reprocess possible.
    """

    __tablename__ = 'document_state_transitions'
    __table_args__ = (
        db.Index('ix_document_state_transitions_doc_created', 'document_id', 'created_at'),
    )

    document_id = db.Column(
        GUID(), db.ForeignKey('documents.id', ondelete='CASCADE'), nullable=False, index=True
    )
    estado_anterior = enum_column(DocumentProcessState, nullable=True)
    estado_nuevo = enum_column(DocumentProcessState, nullable=False)
    tarea = db.Column(db.String(120))
    motivo = db.Column(db.String(500))
    actor_id = db.Column(GUID(), db.ForeignKey('users.id'), nullable=True)
    duracion_ms = db.Column(db.Integer)

    document = db.relationship('Document', back_populates='transitions')
    actor = db.relationship('User', foreign_keys=[actor_id], lazy='select')

    def __repr__(self):
        return f'<DocumentStateTransition {self.estado_anterior}->{self.estado_nuevo}>'

    def to_dict(self):
        return {
            'estado_anterior': str(self.estado_anterior) if self.estado_anterior else None,
            'estado_nuevo': str(self.estado_nuevo),
            'tarea': self.tarea,
            'motivo': self.motivo,
            'duracion_ms': self.duracion_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


def _etiqueta_de_servicio(servicio):
    """One extracted service as a line somebody can scan.

    Defensive about key names: the payload shape differs per document class,
    and a missing key here should cost the label, never the page.
    """
    campos = (servicio or {}).get('campos') or {}

    numero = campos.get('numero_vuelo') or campos.get('numero_tren') or campos.get('numero')
    origen = campos.get('origen_codigo') or campos.get('origen_nombre') or campos.get('origen')
    destino = campos.get('destino_codigo') or campos.get('destino_nombre') or campos.get('destino')
    if origen and destino:
        ruta = f'{origen} → {destino}'
        return f'{numero} {ruta}' if numero else ruta

    nombre = campos.get('nombre') or campos.get('establecimiento') or campos.get('proveedor')
    if nombre:
        ciudad = campos.get('ciudad')
        return f'{nombre} ({ciudad})' if ciudad else str(nombre)

    return str(numero) if numero else None
