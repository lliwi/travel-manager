"""The document processing pipeline (specification section 2.3).

Eight tasks chained in order. Three properties make the chain safe to retry:

* **Idempotent.** Each task exits immediately unless the document is in its
  expected input state, so a Celery redelivery does nothing.
* **Database-driven.** Each task reads its inputs from the database rather than
  from the previous task's return value, which is what lets a failed document
  resume from the step that failed instead of starting over.
* **Explicit failure classes.** ``TransientError`` retries with backoff;
  ``PermanentError`` (infected file, wrong type, hash mismatch, a model that
  will not honour the schema) fails immediately, because retrying cannot help.
"""
import contextlib
import logging

from celery import chain

from app.extensions import db
from app.models.document import Document, DocumentPage, DocumentText
from app.models.enums import (
    AntivirusStatus,
    DocumentClassification,
    DocumentProcessState,
    ExtractionMethod,
    ProvenanceOrigin,
)
from app.services import document_service
from app.tasks.celery_app import celery
from app.utils.errors import PermanentError, TransientError
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

S = DocumentProcessState

#: Shared retry policy. Only transient failures are retried.
RETRY_POLICY = {
    'bind': True,
    'autoretry_for': (TransientError,),
    'retry_backoff': True,
    'retry_backoff_max': 600,
    'retry_jitter': True,
    'max_retries': 3,
}


def _load(document_id):
    """Fetch the document, or None when it has vanished."""
    import uuid

    try:
        return db.session.get(Document, uuid.UUID(str(document_id)))
    except (ValueError, TypeError):
        return None


def _skip_unless(document, *expected):
    """True when this task should do nothing.

    A task whose document is not in its input state has either already run or
    been overtaken; either way, running again would be wrong.
    """
    if document is None:
        logger.warning('Documento inexistente; se omite la tarea.')
        return True
    if document.estado_proceso not in expected:
        logger.info(
            'Se omite la tarea para %s: el documento está en «%s».',
            document.id, document.estado_proceso,
        )
        return True
    return False


# ======================================================================
# 1. Validation
# ======================================================================
@celery.task(name='app.tasks.documents.validate', **RETRY_POLICY)
def validate_document(self, document_id):
    """Check the real file type, size and structure (section 2.3 step 1)."""
    document = _load(document_id)
    if _skip_unless(document, S.RECIBIDO, S.ERROR):
        return document_id

    try:
        from app.services import storage_service

        _, quarantine = storage_service.buckets()
        stream = storage_service.get_backend().get(
            quarantine, document.objeto_cuarentena
        )
        head = stream.read(8192)
        stream.seek(0)

        mime = _sniff_mime(head, document.extension)
        hallazgos = {}

        # The declared type is a claim; the magic bytes are evidence. A file
        # named .pdf that is not a PDF is refused rather than processed.
        allowed = _allowed_mimetypes()
        if mime not in allowed:
            raise PermanentError(
                f'El contenido real del archivo es «{mime}», que no está permitido.'
            )

        if mime == 'application/pdf':
            hallazgos = _inspect_pdf(stream)
            document.paginas = hallazgos.pop('paginas', None)

        document.mime = mime
        document.datos_validacion = hallazgos or None

        document_service.transition(
            document, S.VALIDADO, tarea='validate', commit=True
        )
    except PermanentError as exc:
        # Terminal: a file whose real type is not allowed will not become
        # allowed on a retry, so it goes straight to 'rechazado'.
        document_service.mark_failed(
            document, 'tipo_no_permitido', str(exc), 'validate', estado=S.RECHAZADO
        )
        raise
    except TransientError:
        raise
    except Exception as exc:
        document_service.mark_failed(document, 'error_validacion', str(exc), 'validate')
        raise

    return document_id


def _allowed_mimetypes():
    from flask import current_app

    return set(current_app.config['ALLOWED_DOCUMENT_MIMETYPES'])


#: Magic-byte signatures, checked before anything else touches the file.
_SIGNATURES = (
    (b'%PDF-', 'application/pdf'),
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'II*\x00', 'image/tiff'),
    (b'MM\x00*', 'image/tiff'),
)


def _sniff_mime(head, extension):
    """Determine the real content type from the file's own bytes.

    libmagic first when it is available; the signature table below is the
    fallback, so a deployment without libmagic still refuses a disguised file
    rather than trusting the declared type.
    """
    with contextlib.suppress(Exception):
        import magic

        detected = magic.from_buffer(head, mime=True)
        if detected:
            return detected

    for signature, mime in _SIGNATURES:
        if head.startswith(signature):
            return mime

    # An EML has no magic number; it is recognised by its headers.
    if extension == 'eml' or _looks_like_email(head):
        return 'message/rfc822'

    return 'application/octet-stream'


def _looks_like_email(head):
    text = head[:2048].decode('utf-8', errors='ignore').lower()
    return any(
        marker in text
        for marker in ('from:', 'to:', 'subject:', 'message-id:', 'received:')
    )


def _inspect_pdf(stream):
    """Page count plus a note of features worth flagging."""
    hallazgos = {}
    try:
        from pypdf import PdfReader

        reader = PdfReader(stream)
        if reader.is_encrypted:
            raise PermanentError(
                'El PDF está cifrado y no puede procesarse. Envíe una versión sin '
                'contraseña.'
            )
        hallazgos['paginas'] = len(reader.pages)

        # Not grounds for rejection -- travel documents do sometimes carry
        # attachments -- but the manager should know it is there.
        root = reader.trailer.get('/Root', {})
        # Advisory only: travel documents do sometimes carry attachments, so
        # these are reported to the manager rather than used to reject.
        for marker, label in (
            ('/JavaScript', 'javascript'),
            ('/OpenAction', 'accion_automatica'),
            ('/EmbeddedFiles', 'archivos_incrustados'),
        ):
            if marker in str(root.keys()):
                hallazgos.setdefault('avisos', []).append(label)
    except PermanentError:
        raise
    except Exception as exc:
        logger.warning('No se pudo inspeccionar el PDF: %s', exc)
    finally:
        if hasattr(stream, 'seek'):
            stream.seek(0)
    return hallazgos


# ======================================================================
# 2. Antivirus
# ======================================================================
@celery.task(name='app.tasks.documents.scan', **RETRY_POLICY)
def scan_document(self, document_id):
    """Scan the file before anything else reads it (section 2.3 step 1)."""
    document = _load(document_id)
    if _skip_unless(document, S.VALIDADO):
        return document_id

    from app.services import antivirus_service, storage_service

    _, quarantine = storage_service.buckets()
    stream = storage_service.get_backend().get(quarantine, document.objeto_cuarentena)

    try:
        result = antivirus_service.scan(stream)
    except TransientError:
        # The scanner is unreachable. Retried by the policy; on no account is
        # the file promoted unscanned.
        raise
    except Exception as exc:
        document.antivirus_estado = AntivirusStatus.ERROR
        document_service.mark_failed(document, 'error_antivirus', str(exc), 'scan')
        raise

    document.antivirus_motor = result.motor
    document.antivirus_at = utcnow()

    if not result.limpio:
        document.antivirus_estado = AntivirusStatus.INFECTADO
        document.antivirus_firma = result.firma
        # The infected bytes are destroyed immediately and never promoted.
        storage_service.get_backend().delete(quarantine, document.objeto_cuarentena)
        document.objeto_cuarentena = None
        document_service.transition(
            document, S.INFECTADO, tarea='scan',
            motivo=f'Detectado: {result.firma}',
        )
        _notify_infected(document, result)
        raise PermanentError(f'Archivo infectado: {result.firma}')

    document.antivirus_estado = (
        AntivirusStatus.OMITIDO if result.motor == 'noop' else AntivirusStatus.LIMPIO
    )
    document_service.transition(document, S.ESCANEADO, tarea='scan', commit=True)
    return document_id


def _notify_infected(document, result):
    """Record an infected upload in the audit trail and as a trip alert."""
    from app.models.enums import AuditResourceType
    from app.services import audit_service

    audit_service.record(
        'document.infected',
        recurso_tipo=AuditResourceType.DOCUMENTO,
        recurso_id=str(document.id),
        metadatos={
            'trip_id': str(document.trip_id),
            'firma': result.firma,
            'motor': result.motor,
            'nombre': document.nombre_original,
        },
    )
    logger.error(
        'Archivo infectado rechazado: %s (%s) en el viaje %s',
        document.nombre_original, result.firma, document.trip_id,
    )


# ======================================================================
# 3. Permanent storage
# ======================================================================
@celery.task(name='app.tasks.documents.store', **RETRY_POLICY)
def store_document(self, document_id):
    """Promote the scanned file out of quarantine, verifying its hash."""
    document = _load(document_id)
    if _skip_unless(document, S.ESCANEADO):
        return document_id

    from app.services import storage_service
    from app.utils.errors import StorageError

    try:
        bucket, key = storage_service.promote(document, verify_hash=True)
    except StorageError as exc:
        document_service.mark_failed(document, 'error_almacenamiento', str(exc), 'store')
        raise PermanentError(str(exc)) from exc

    document.bucket = bucket
    document.objeto_storage = key
    document.objeto_cuarentena = None

    document_service.transition(document, S.ALMACENADO, tarea='store', commit=True)
    return document_id


# ======================================================================
# 4. Text extraction and OCR
# ======================================================================
@celery.task(name='app.tasks.documents.extract_text', **RETRY_POLICY)
def extract_text(self, document_id):
    """Read the document's text, using OCR only when there is none."""
    document = _load(document_id)
    if _skip_unless(document, S.ALMACENADO):
        return document_id

    from app.services import ocr_service, storage_service

    try:
        stream = storage_service.open_document(document)
        extracted = ocr_service.extract(
            stream, document.mime, document.nombre_original
        )
    except PermanentError as exc:
        document_service.mark_failed(document, 'sin_texto', str(exc), 'extract_text')
        raise
    except Exception as exc:
        document_service.mark_failed(
            document, 'error_extraccion_texto', str(exc), 'extract_text'
        )
        raise

    DocumentText.query.filter_by(document_id=document.id).delete()
    DocumentPage.query.filter_by(document_id=document.id).delete()

    db.session.add(DocumentText(
        document_id=document.id,
        contenido=extracted.contenido,
        caracteres=extracted.caracteres,
        motor=extracted.motor,
    ))
    for page in extracted.pages:
        db.session.add(DocumentPage(
            document_id=document.id,
            numero=page.numero,
            contenido=page.contenido,
            caracteres=page.caracteres,
            uso_ocr=page.uso_ocr,
            confianza_ocr=page.confianza,
        ))

    document.tiene_texto_extraible = extracted.tiene_texto
    document.uso_ocr = extracted.uso_ocr
    document.idioma_detectado = extracted.idioma
    if document.paginas is None:
        document.paginas = len(extracted)

    document_service.transition(
        document, S.TEXTO_EXTRAIDO, tarea='extract_text', commit=True
    )
    return document_id


# ======================================================================
# 5. Classification
# ======================================================================
@celery.task(name='app.tasks.documents.classify', **RETRY_POLICY)
def classify_document(self, document_id):
    """Classify the document, using rules first and a model only if unsure."""
    document = _load(document_id)
    if _skip_unless(document, S.TEXTO_EXTRAIDO):
        return document_id

    from app.services.classification_service import classify_by_rules

    texto = document.texto.contenido if document.texto else ''
    clasificacion, confianza = classify_by_rules(texto, document.nombre_original)

    origen = ProvenanceOrigin.DOCUMENTO
    threshold = _classification_threshold()

    if confianza < threshold:
        # Rules were not sure; ask a model, but keep the rules' answer if the
        # model is not confident either.
        try:
            from app.services import ai_service

            result = ai_service.classify_document(document)
            datos = result['datos']
            ai_confianza = float(datos.get('confianza') or 0)
            if ai_confianza > confianza:
                clasificacion = DocumentClassification.coerce(
                    datos.get('clasificacion'), clasificacion
                )
                confianza = ai_confianza
                origen = ProvenanceOrigin.IA
        except Exception as exc:
            logger.warning(
                'No se pudo clasificar con IA el documento %s: %s', document.id, exc
            )

    document.clasificacion = clasificacion
    document.clasificacion_confianza = round(confianza, 2)
    document.clasificacion_origen = origen

    document_service.transition(document, S.CLASIFICADO, tarea='classify', commit=True)
    return document_id


def _classification_threshold():
    return 0.7


# ======================================================================
# 6. AI extraction
# ======================================================================
@celery.task(name='app.tasks.documents.ai_extract', **RETRY_POLICY)
def ai_extract_document(self, document_id):
    """Pull structured fields out of the text (section 2.3 step 4)."""
    document = _load(document_id)
    if _skip_unless(document, S.CLASIFICADO):
        return document_id

    from app.services import ai_service, extraction_service
    from app.utils.errors import AIContractError, AIError, AIPolicyBlocked

    try:
        result = ai_service.extract_document(document, document.clasificacion)
    except AIPolicyBlocked as exc:
        document_service.mark_failed(
            document, 'politica_ia', exc.mensaje, 'ai_extract'
        )
        raise PermanentError(exc.mensaje) from exc
    except AIContractError as exc:
        document_service.mark_failed(
            document, 'contrato_ia', str(exc), 'ai_extract'
        )
        raise PermanentError(str(exc)) from exc
    except AIError as exc:
        document_service.mark_failed(document, 'error_ia', str(exc), 'ai_extract')
        raise

    datos = result['datos']
    run = result['run']

    extraction_service.create_version(
        document,
        payload=datos,
        metodo=ExtractionMethod.IA,
        proveedor=str(run.proveedor),
        modelo=run.modelo,
        ai_run_id=run.id,
        confianzas=datos.get('confianzas') or {},
        confianza_global=datos.get('confianza_global'),
        avisos=datos.get('avisos') or [],
        commit=False,
    )

    document_service.transition(document, S.EXTRAIDO, tarea='ai_extract', commit=True)
    return document_id


# ======================================================================
# 7. Normalisation
# ======================================================================
@celery.task(name='app.tasks.documents.normalize', **RETRY_POLICY)
def normalize_extraction(self, document_id):
    """Normalise dates, timezones, codes and locators (section 2.3 step 5)."""
    document = _load(document_id)
    if _skip_unless(document, S.EXTRAIDO):
        return document_id

    from app.services import extraction_service, normalization_service

    extraction = document.current_extraction
    if extraction is None:
        document_service.mark_failed(
            document, 'sin_extraccion', 'No hay ninguna extracción que normalizar.',
            'normalize',
        )
        raise PermanentError('No hay ninguna extracción que normalizar.')

    result = normalization_service.normalize(
        extraction.payload, str(document.clasificacion)
    )
    extraction_service.set_normalized(extraction, result, commit=False)

    document_service.transition(document, S.NORMALIZADO, tarea='normalize', commit=True)
    return document_id


# ======================================================================
# 8. Hand over for review
# ======================================================================
@celery.task(name='app.tasks.documents.finalize', **RETRY_POLICY)
def finalize_for_review(self, document_id):
    """Put the extraction in front of a manager (section 2.3 step 6)."""
    document = _load(document_id)
    if _skip_unless(document, S.NORMALIZADO):
        return document_id

    from app.models.enums import ExtractionState
    from app.services import settings_service

    extraction = document.current_extraction
    if extraction is not None:
        extraction.estado = ExtractionState.EN_REVISION

    document_service.transition(
        document, S.PENDIENTE_REVISION, tarea='finalize', commit=True
    )

    # Auto-approval exists but ships disabled: phase 1 delivers mandatory review
    # as section 2.3 step 6 describes.
    if (
        settings_service.get_bool('DOCUMENTOS_AUTO_APROBAR', False)
        and extraction is not None
        and extraction.confianza_global is not None
        and float(extraction.confianza_global)
        >= settings_service.get_float('DOCUMENTOS_UMBRAL_AUTO_APROBAR', 0.95)
        and not extraction.avisos
    ):
        logger.info(
            'Aprobación automática de la extracción %s (confianza %.2f).',
            extraction.id, float(extraction.confianza_global),
        )
        from app.services import extraction_service

        extraction_service.approve(document.subido_por, extraction,
                                   comentario='Aprobación automática por confianza alta.')

    return document_id


# ======================================================================
# Chain construction
# ======================================================================
#: Pipeline steps in order, keyed by the name ``reprocess`` resumes from.
PIPELINE = (
    ('validate', validate_document),
    ('scan', scan_document),
    ('store', store_document),
    ('extract_text', extract_text),
    ('classify', classify_document),
    ('ai_extract', ai_extract_document),
    ('normalize', normalize_extraction),
    ('finalize', finalize_for_review),
)


def build_pipeline(document_id, start_from=None):
    """Build the Celery chain, optionally resuming partway through."""
    names = [name for name, _ in PIPELINE]
    start = names.index(start_from) if start_from in names else 0
    steps = [task.si(str(document_id)) for _, task in PIPELINE[start:]]
    return chain(*steps)
