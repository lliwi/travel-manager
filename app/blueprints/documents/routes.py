"""Document routes (Jinja surface).

Upload, processing status, the review screen and downloads. The pipeline itself
lives in ``app.tasks.document_tasks``; these routes only start it and render its
state.
"""
import logging

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.documents import documents_bp
from app.models.enums import DocumentProcessState
from app.services import document_service
from app.services.authorization_service import Permiso
from app.utils.decorators import require_document_access, require_trip_access
from app.utils.errors import AppError

logger = logging.getLogger(__name__)


@documents_bp.route('/viaje/<trip_id>')
@login_required
@require_trip_access(Permiso.VER_DOCUMENTO)
def index(trip_id, trip):
    """List a trip's documents."""
    documents = document_service.list_for_trip(
        current_user._get_current_object(), trip
    )
    return render_template(
        'documents/index.html',
        trip=trip,
        documents=documents,
        DocumentProcessState=DocumentProcessState,
    )


@documents_bp.route('/viaje/<trip_id>/subir', methods=['GET', 'POST'])
@login_required
@require_trip_access(Permiso.SUBIR_DOCUMENTO)
def upload(trip_id, trip):
    """Attach a document to a trip and start the processing pipeline."""
    from app.blueprints.documents.forms import DocumentUploadForm

    form = DocumentUploadForm()
    form.trip_traveler_id.choices = [('', '— Todo el viaje —')] + [
        (str(t.id), t.nombre_completo) for t in trip.active_travelers
    ]

    if form.validate_on_submit():
        from app.models.enums import DocumentType

        try:
            document = document_service.upload(
                actor=current_user._get_current_object(),
                trip=trip,
                file_storage=form.archivo.data,
                tipo=DocumentType.coerce(form.tipo.data, DocumentType.OTRO),
                trip_traveler_id=form.trip_traveler_id.data or None,
            )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return render_template('documents/upload.html', trip=trip, form=form)

        flash(
            f'Documento «{document.nombre_original}» recibido. '
            'Se está procesando en segundo plano.',
            'success',
        )
        return redirect(url_for('documents.detail', document_id=document.id))

    return render_template('documents/upload.html', trip=trip, form=form)


@documents_bp.route('/<document_id>')
@login_required
@require_document_access(Permiso.VER_DOCUMENTO)
def detail(document_id, document):
    """Document detail with its processing state and extracted data."""
    return render_template(
        'documents/detail.html',
        document=document,
        trip=document.trip,
        extraction=document.current_extraction,
    )


@documents_bp.route('/<document_id>/descargar')
@login_required
@require_document_access(Permiso.DESCARGAR_DOCUMENTO)
def download(document_id, document):
    """Download the original file.

    Audited before the response is produced, so the record exists even if the
    transfer is interrupted.
    """
    try:
        return document_service.download_response(
            current_user._get_current_object(), document
        )
    except AppError as error:
        flash(error.mensaje, 'danger')
        return redirect(url_for('documents.detail', document_id=document.id))


@documents_bp.route('/<document_id>/revision', methods=['GET', 'POST'])
@login_required
@require_document_access(Permiso.REVISAR_EXTRACCION)
def review(document_id, document):
    """Accept, correct or discard the extracted fields (section 2.3 step 6)."""
    from app.services import extraction_service

    extraction = document.current_extraction
    if extraction is None:
        flash('Este documento todavía no tiene datos extraídos.', 'info')
        return redirect(url_for('documents.detail', document_id=document.id))

    if request.method == 'POST':
        accion = request.form.get('accion', 'aprobar')
        actor = current_user._get_current_object()
        try:
            if accion == 'descartar':
                extraction_service.reject(
                    actor, extraction, comentario=request.form.get('comentario')
                )
                flash('Extracción descartada.', 'info')
            else:
                correcciones = extraction_service.parse_review_form(
                    extraction, request.form
                )
                resultado = extraction_service.approve(
                    actor, extraction,
                    correcciones=correcciones,
                    comentario=request.form.get('comentario'),
                )
                cuantos = len(resultado.get('entities') or [])
                flash(
                    f'{cuantos} servicios aplicados al itinerario.' if cuantos != 1
                    else 'Datos aprobados y aplicados al itinerario.',
                    'success',
                )
        except AppError as error:
            flash(error.mensaje, 'danger')
            return redirect(url_for('documents.review', document_id=document.id))

        return redirect(url_for('trips.detail', trip_id=document.trip_id))

    return render_template(
        'documents/review.html',
        document=document,
        trip=document.trip,
        extraction=extraction,
        campos=extraction_service.review_fields(extraction),
    )


@documents_bp.route('/<document_id>/reprocesar', methods=['POST'])
@login_required
@require_document_access(Permiso.SUBIR_DOCUMENTO)
def reprocess(document_id, document):
    """Retry a failed pipeline from the last good state."""
    try:
        document_service.reprocess(current_user._get_current_object(), document)
        flash('Reproceso iniciado.', 'info')
    except AppError as error:
        flash(error.mensaje, 'danger')
    return redirect(url_for('documents.detail', document_id=document.id))


@documents_bp.route('/<document_id>/eliminar', methods=['POST'])
@login_required
@require_document_access(Permiso.ELIMINAR_DOCUMENTO)
def delete(document_id, document):
    """Soft-delete a document. The original stays in storage until purged."""
    trip_id = document.trip_id
    document_service.delete(current_user._get_current_object(), document)
    flash('Documento eliminado.', 'info')
    return redirect(url_for('documents.index', trip_id=trip_id))
