"""Document intake, state machine and downloads.

Specification section 2.3. The state machine is the important part: no other
module may assign ``documents.estado_proceso``. Every move goes through
:func:`transition`, which validates it against :data:`ALLOWED_TRANSITIONS` and
records it in ``document_state_transitions`` -- which is what makes a failed
pipeline diagnosable and a partial reprocess possible.
"""
import logging
import os
import time

from flask import current_app, send_file
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models.document import Document
from app.models.enums import (
    DOCUMENT_TERMINAL_STATES,
    AntivirusStatus,
    AuditResourceType,
    DocumentClassification,
    DocumentProcessState,
    DocumentType,
)
from app.services import audit_service, storage_service
from app.utils.errors import (
    ConflictError,
    InvalidTransition,
    PayloadTooLarge,
    ResourceNotFound,
    ValidationError,
)
from app.utils.hashing import sha256_stream
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)

S = DocumentProcessState

#: The only legal moves through the pipeline. Anything else is a bug, and
#: raising on it surfaces that bug at the point it happens rather than leaving a
#: document silently stuck.
ALLOWED_TRANSITIONS = {
    S.RECIBIDO: {S.VALIDADO, S.RECHAZADO, S.ERROR, S.DESCARTADO},
    S.VALIDADO: {S.ESCANEADO, S.INFECTADO, S.ERROR, S.DESCARTADO},
    S.ESCANEADO: {S.ALMACENADO, S.ERROR, S.DESCARTADO},
    S.ALMACENADO: {S.TEXTO_EXTRAIDO, S.ERROR, S.DESCARTADO},
    S.TEXTO_EXTRAIDO: {S.CLASIFICADO, S.ERROR, S.DESCARTADO},
    S.CLASIFICADO: {S.EXTRAIDO, S.ERROR, S.DESCARTADO},
    S.EXTRAIDO: {S.NORMALIZADO, S.ERROR, S.DESCARTADO},
    S.NORMALIZADO: {S.PENDIENTE_REVISION, S.REVISADO, S.ERROR, S.DESCARTADO},
    S.PENDIENTE_REVISION: {S.REVISADO, S.APROBADO, S.DESCARTADO, S.ERROR},
    S.REVISADO: {S.APROBADO, S.PENDIENTE_REVISION, S.DESCARTADO},
    S.APROBADO: {S.PENDIENTE_REVISION, S.DESCARTADO},
    # A failed document may be retried from the step that failed.
    S.ERROR: {
        S.RECIBIDO, S.VALIDADO, S.ESCANEADO, S.ALMACENADO, S.TEXTO_EXTRAIDO,
        S.CLASIFICADO, S.EXTRAIDO, S.NORMALIZADO, S.DESCARTADO,
    },
    # Terminal.
    S.INFECTADO: set(),
    S.RECHAZADO: set(),
    S.DESCARTADO: set(),
}


def transition(document, nuevo, tarea=None, actor=None, motivo=None,
               duracion_ms=None, retroceso=False, commit=True):
    """Move a document to a new pipeline state.

    The *only* supported way to change ``estado_proceso``. A test asserts that
    no other module assigns that attribute.

    Args:
        retroceso: Allow moving *backwards* along the pipeline, which is what
            reprocessing needs. Every task refuses to act unless the document is
            in its own input state -- the property that makes a Celery
            redelivery harmless -- so without rewinding first, reprocessing a
            finished document runs the whole chain and changes nothing. Still
            only backwards, still recorded, still never out of a terminal state.

    Raises:
        InvalidTransition: The move is not allowed from the current state.
    """
    actual = document.estado_proceso
    nuevo = DocumentProcessState.coerce(nuevo)
    if nuevo is None:
        raise ValidationError('Estado de proceso no válido.')

    if actual is nuevo:
        return document

    if retroceso:
        _validar_retroceso(actual, nuevo)
    else:
        allowed = ALLOWED_TRANSITIONS.get(actual, set())
        if nuevo not in allowed:
            raise InvalidTransition(
                f'No se permite pasar de «{actual.label}» a «{nuevo.label}».',
                detalles={'actual': str(actual), 'solicitado': str(nuevo)},
            )

    from app.models.document import DocumentStateTransition

    document.estado_proceso = nuevo
    db.session.add(DocumentStateTransition(
        document_id=document.id,
        estado_anterior=actual,
        estado_nuevo=nuevo,
        tarea=tarea,
        motivo=(motivo[:500] if motivo else None),
        actor_id=getattr(actor, 'id', None),
        duracion_ms=duracion_ms,
    ))

    if nuevo is not S.ERROR:
        document.error_codigo = None
        document.error_mensaje = None

    if commit:
        db.session.commit()
    return document


def _validar_retroceso(actual, nuevo):
    """Check that a rewind goes backwards along the pipeline and nowhere else."""
    from app.models.enums import DOCUMENT_PIPELINE_ORDER

    if actual in DOCUMENT_TERMINAL_STATES:
        raise InvalidTransition(
            f'Un documento en estado «{actual.label}» no puede reprocesarse.'
        )

    if nuevo not in DOCUMENT_PIPELINE_ORDER:
        raise InvalidTransition(
            f'«{nuevo.label}» no es un paso del proceso al que se pueda volver.'
        )

    # ERROR sits outside the happy path, so any pipeline step is "earlier".
    if actual is S.ERROR:
        return

    if actual not in DOCUMENT_PIPELINE_ORDER:
        raise InvalidTransition(
            f'No se puede reprocesar desde «{actual.label}».'
        )

    if DOCUMENT_PIPELINE_ORDER.index(nuevo) >= DOCUMENT_PIPELINE_ORDER.index(actual):
        raise InvalidTransition(
            f'«{nuevo.label}» no es anterior a «{actual.label}»; un reproceso '
            'solo retrocede.'
        )


def mark_failed(document, codigo, mensaje, tarea=None, estado=None, commit=True):
    """Record a pipeline failure without losing where it got to.

    Args:
        estado: The state to move to. Defaults to ``ERROR``, which is
            retryable. Pass a terminal state such as ``RECHAZADO`` when
            retrying could not possibly help.
    """
    document.error_codigo = str(codigo)[:60]
    # Truncated and never the raw exception text of a document's contents:
    # section 3.2 wants logs free of unnecessary PII.
    document.error_mensaje = str(mensaje)[:500]
    document.intentos = (document.intentos or 0) + 1

    try:
        transition(document, estado or S.ERROR, tarea=tarea,
                   motivo=str(mensaje)[:500], commit=False)
    except InvalidTransition:
        # Already terminal; keep the error detail, leave the state alone.
        pass

    if commit:
        db.session.commit()

    audit_service.record(
        'document.processing_failed',
        recurso_tipo=AuditResourceType.DOCUMENTO,
        recurso_id=str(document.id),
        metadatos={'codigo': codigo, 'tarea': tarea},
    )
    return document


# ======================================================================
# Upload
# ======================================================================
def upload(actor, trip, file_storage, tipo=DocumentType.OTRO,
           trip_traveler_id=None, commit=True):
    """Accept an uploaded file and start the pipeline.

    Validation here is the cheap part -- extension, size, duplicate. The real
    checks (magic bytes, antivirus) happen in the pipeline, because they must
    also apply to anything that reaches the system another way.
    """
    if file_storage is None or not getattr(file_storage, 'filename', None):
        raise ValidationError('No se ha recibido ningún archivo.')

    original_name = secure_filename(file_storage.filename) or 'documento'
    extension = os.path.splitext(original_name)[1].lstrip('.').lower()

    allowed = current_app.config['ALLOWED_DOCUMENT_EXTENSIONS']
    if extension not in allowed:
        raise ValidationError(
            f'Formato no admitido: .{extension}. Se aceptan: {", ".join(allowed)}.'
        )

    digest, size = sha256_stream(file_storage.stream)
    if size == 0:
        raise ValidationError('El archivo está vacío.')

    max_bytes = current_app.config['MAX_DOCUMENT_BYTES']
    if size > max_bytes:
        raise PayloadTooLarge(
            f'El archivo ocupa {size // 1024 // 1024} MB y el máximo es '
            f'{max_bytes // 1024 // 1024} MB.'
        )

    duplicate = Document.query.filter_by(
        trip_id=trip.id, hash_sha256=digest, is_deleted=False
    ).first()
    if duplicate is not None:
        raise ConflictError(
            'Este archivo ya está adjunto a este viaje '
            f'(«{duplicate.nombre_original}»).'
        )

    document = Document(
        trip_id=trip.id,
        trip_traveler_id=trip_traveler_id or None,
        nombre_original=file_storage.filename[:400],
        tipo=tipo,
        mime_declarado=file_storage.mimetype,
        extension=extension,
        tamano_bytes=size,
        hash_sha256=digest,
        estado_proceso=S.RECIBIDO,
        antivirus_estado=AntivirusStatus.PENDIENTE,
        clasificacion=DocumentClassification.DESCONOCIDO,
        subido_por_id=actor.id,
        subido_en=utcnow(),
        storage_backend=current_app.config['STORAGE_BACKEND'],
    )
    db.session.add(document)
    db.session.flush()

    bucket, key = storage_service.store_quarantine(
        document.id, extension, file_storage.stream,
        content_type=file_storage.mimetype,
    )
    document.objeto_cuarentena = key
    document.retencion_hasta = _retention_deadline()

    # Audited whether or not this call commits: an upload that the caller
    # commits as part of a larger transaction is still an upload.
    audit_service.record(
        'document.uploaded',
        recurso_tipo=AuditResourceType.DOCUMENTO,
        recurso_id=str(document.id),
        actor=actor,
        metadatos={
            'trip_id': str(trip.id),
            'nombre': document.nombre_original,
            'tamano_bytes': size,
            'hash': digest,
        },
        commit=False,
    )

    if commit:
        db.session.commit()
        _enqueue_pipeline(document)

    return document


def _enqueue_pipeline(document, start_from=None):
    """Hand the document to the Celery pipeline.

    Failure to enqueue is logged and surfaced on the document rather than raised:
    the file is already safely in quarantine, and a queue outage should not lose
    the upload.
    """
    from app.tasks.dispatch import is_eager

    if is_eager():
        return _run_pipeline_inline(document, start_from)

    try:
        from app.tasks.document_tasks import build_pipeline

        chain = build_pipeline(str(document.id), start_from=start_from)
        result = chain.apply_async()
        document.celery_task_id = result.id
        db.session.commit()
        return result
    except Exception as exc:
        logger.exception('No se pudo encolar el procesamiento del documento %s',
                         document.id)
        document.error_codigo = 'cola_no_disponible'
        document.error_mensaje = str(exc)[:500]
        db.session.commit()
        return None


def _run_pipeline_inline(document, start_from=None):
    """Run the whole pipeline synchronously, for tests and broker outages.

    Each step is invoked directly rather than through the broker. A failure
    stops the chain exactly as the asynchronous version would, and the document
    keeps the state it reached.
    """
    from app.tasks.document_tasks import PIPELINE

    names = [name for name, _ in PIPELINE]
    start = names.index(start_from) if start_from in names else 0

    document_id = str(document.id)
    for _, task in PIPELINE[start:]:
        try:
            task.run(document_id)
        except Exception as exc:
            logger.info(
                'El procesamiento en línea del documento %s se detuvo en %s: %s',
                document_id, task.name, exc,
            )
            break
    db.session.refresh(document)
    return None


def _retention_deadline():
    """When this original may be purged, per the retention policy."""
    from datetime import timedelta

    from app.services import settings_service

    days = settings_service.get_int('RETENCION_DOCUMENTOS_DIAS', 1825)
    return utcnow() + timedelta(days=days)


# ======================================================================
# Reprocessing
# ======================================================================
#: Which pipeline step each state should resume from.
RESUME_FROM = {
    S.RECIBIDO: 'validate',
    S.VALIDADO: 'scan',
    S.ESCANEADO: 'store',
    S.ALMACENADO: 'extract_text',
    S.TEXTO_EXTRAIDO: 'classify',
    S.CLASIFICADO: 'ai_extract',
    S.EXTRAIDO: 'normalize',
    S.NORMALIZADO: 'finalize',
}


def reprocess(actor, document, from_start=False):
    """Retry a failed document from the last state it reached.

    Possible only because every task reads its inputs from the database rather
    than from the previous task's return value.
    """
    if document.estado_proceso in DOCUMENT_TERMINAL_STATES:
        raise ConflictError(
            f'Un documento en estado «{document.estado_proceso.label}» no puede '
            'reprocesarse.'
        )

    if from_start:
        destino = _earliest_replayable_state(document)
    else:
        destino = _last_successful_state(document)
    start_from = RESUME_FROM.get(destino, 'validate')

    # Rewind first. Each task exits without acting unless the document is in
    # its own input state, so enqueuing the chain over a finished document
    # would run every step and change nothing at all.
    if document.estado_proceso is not destino:
        transition(
            document, destino, tarea='reprocess', actor=actor,
            motivo='Reproceso solicitado', retroceso=True, commit=False,
        )

    document.error_codigo = None
    document.error_mensaje = None
    db.session.commit()

    audit_service.record(
        'document.reprocess_requested',
        recurso_tipo=AuditResourceType.DOCUMENTO,
        recurso_id=str(document.id),
        actor=actor,
        metadatos={'desde': start_from},
    )
    _enqueue_pipeline(document, start_from=start_from)
    return document


#: Furthest point a reprocess rewinds to. Beyond this the pipeline has nothing
#: left to do, so a document that already reached review is sent back to
#: classification -- which is what a manager wants after changing the model or
#: correcting the classification.
_REPROCESS_CEILING = S.CLASIFICADO


def _earliest_replayable_state(document):
    """How far back a full reprocess can actually go.

    "From the start" is bounded by what is still on disk. Promotion deletes the
    quarantine object once the definitive one is stored and its hash verified,
    so validation and the antivirus scan -- both of which read the quarantine
    copy -- have nothing left to read. Rewinding past that point sends the
    document to a task that can only fail, which is how a reprocess turned a
    reviewable document into an errored one.
    """
    if document.objeto_cuarentena:
        return S.RECIBIDO
    if document.objeto_storage:
        return S.ALMACENADO
    raise ConflictError(
        'No queda copia del fichero original, así que el documento no puede '
        'reprocesarse.'
    )


def _last_successful_state(document):
    """Where a reprocess should resume from.

    The furthest pipeline state this document actually reached, capped so that
    a finished document rewinds far enough to produce a new extraction rather
    than replaying steps that would all decline to act.
    """
    from app.models.enums import DOCUMENT_PIPELINE_ORDER

    reached = {
        t.estado_nuevo for t in document.transitions
        if t.estado_nuevo in DOCUMENT_PIPELINE_ORDER
    }

    best = S.RECIBIDO
    for state in DOCUMENT_PIPELINE_ORDER:
        if state in reached:
            best = state

    tope = DOCUMENT_PIPELINE_ORDER.index(_REPROCESS_CEILING)
    if DOCUMENT_PIPELINE_ORDER.index(best) > tope:
        return _REPROCESS_CEILING
    return best


# ======================================================================
# Reading
# ======================================================================
def list_for_trip(actor, trip, include_deleted=False):
    """A trip's documents, scoped to what the actor may see.

    A traveller sees documents addressed to them plus documents that apply to
    the whole trip -- the same rule the itinerary uses.
    """
    from app.models.enums import RoleCode

    query = Document.query.filter(Document.trip_id == trip.id)
    if not include_deleted:
        query = query.filter(Document.is_deleted.is_(False))

    if not (actor.has_role(RoleCode.ADMINISTRADOR) or actor.has_role(RoleCode.GESTOR)):
        traveler = trip.traveler_for(actor.id)
        if traveler is None:
            return []
        query = query.filter(
            db.or_(
                Document.trip_traveler_id.is_(None),
                Document.trip_traveler_id == traveler.id,
            )
        )

    return query.order_by(Document.created_at.desc()).all()


def get_or_404(document_id):
    """Fetch a document by id or raise :class:`ResourceNotFound`."""
    import uuid

    try:
        document = db.session.get(Document, uuid.UUID(str(document_id)))
    except (ValueError, TypeError):
        document = None
    if document is None or document.is_deleted:
        raise ResourceNotFound('El documento solicitado no existe.')
    return document


def download_response(actor, document):
    """Produce the download response, auditing before any bytes move.

    The audit entry is written *first*: a record of an interrupted download is
    correct, while a download with no record is not.
    """
    if not document.esta_disponible:
        raise ConflictError(
            'El documento no está disponible para su descarga.'
        )

    audit_service.record(
        'document.downloaded',
        recurso_tipo=AuditResourceType.DOCUMENTO,
        recurso_id=str(document.id),
        actor=actor,
        metadatos={
            'trip_id': str(document.trip_id),
            'nombre': document.nombre_original,
            'hash': document.hash_sha256,
        },
    )

    if not current_app.config['STORAGE_STREAM_THROUGH_APP']:
        url = storage_service.presigned_download(document)
        if url:
            from flask import redirect

            return redirect(url)

    stream = storage_service.open_document(document)
    response = send_file(
        stream,
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name=document.nombre_original,
    )
    # Never let an uploaded SVG or HTML render on our own origin.
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response


def delete(actor, document, commit=True):
    """Soft-delete a document.

    The stored original is *not* removed: the retention policy governs that, and
    the hash must stay verifiable for as long as the trail references it.
    """
    document.soft_delete(actor)
    if commit:
        db.session.commit()
        audit_service.record(
            'document.deleted',
            recurso_tipo=AuditResourceType.DOCUMENTO,
            recurso_id=str(document.id),
            actor=actor,
            metadatos={'nombre': document.nombre_original},
        )
    return document


class timed:
    """Context manager measuring a pipeline step in milliseconds."""

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.monotonic() - self._start) * 1000)
        return False
