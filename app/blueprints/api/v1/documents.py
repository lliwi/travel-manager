"""Document endpoints (specification section 7)."""
from flask import request
from flask_login import current_user, login_required

from app.blueprints.api.v1 import api_v1_bp, ok
from app.services import document_service, extraction_service
from app.services.authorization_service import Permiso
from app.utils.decorators import require_document_access, require_trip_access
from app.utils.errors import ValidationError


@api_v1_bp.route('/trips/<trip_id>/documents', methods=['GET'])
@login_required
@require_trip_access(Permiso.VER_DOCUMENTO)
def list_documents(trip_id, trip):
    """A trip's documents."""
    actor = current_user._get_current_object()
    documents = document_service.list_for_trip(actor, trip)
    return ok([d.to_dict() for d in documents])


@api_v1_bp.route('/trips/<trip_id>/documents', methods=['POST'])
@login_required
@require_trip_access(Permiso.SUBIR_DOCUMENTO)
def upload_document(trip_id, trip):
    """Attach a document and start the processing pipeline."""
    from app.models.enums import DocumentType

    if 'archivo' not in request.files and 'file' not in request.files:
        raise ValidationError('Se requiere un archivo en el campo «archivo».')

    file_storage = request.files.get('archivo') or request.files.get('file')
    document = document_service.upload(
        actor=current_user._get_current_object(),
        trip=trip,
        file_storage=file_storage,
        tipo=DocumentType.coerce(request.form.get('tipo'), DocumentType.OTRO),
        trip_traveler_id=request.form.get('trip_traveler_id') or None,
    )
    return ok(document.to_dict(), status=202)


@api_v1_bp.route('/documents/<document_id>', methods=['GET'])
@login_required
@require_document_access(Permiso.VER_DOCUMENTO)
def get_document(document_id, document):
    """One document, its pipeline state and its current extraction."""
    data = document.to_dict(include_transitions=True)
    extraction = document.current_extraction
    if extraction is not None:
        data['extraccion'] = extraction.to_dict()
    return ok(data)


@api_v1_bp.route('/documents/<document_id>/download', methods=['GET'])
@login_required
@require_document_access(Permiso.DESCARGAR_DOCUMENTO)
def download_document(document_id, document):
    """Download the original file. Audited before the response is produced."""
    return document_service.download_response(
        current_user._get_current_object(), document
    )


@api_v1_bp.route('/documents/<document_id>/review', methods=['POST'])
@login_required
@require_document_access(Permiso.REVISAR_EXTRACCION)
def review_document(document_id, document):
    """Approve, correct or discard the extracted data (section 2.3 step 6)."""
    payload = request.get_json(silent=True) or {}
    extraction = document.current_extraction
    if extraction is None:
        raise ValidationError('Este documento no tiene datos extraídos que revisar.')

    actor = current_user._get_current_object()
    accion = payload.get('accion', 'aprobar')

    if accion == 'descartar':
        extraction_service.reject(actor, extraction, comentario=payload.get('comentario'))
        return ok({'mensaje': 'Extracción descartada.', 'estado': str(extraction.estado)})

    result = extraction_service.approve(
        actor,
        extraction,
        correcciones=payload.get('correcciones') or {},
        comentario=payload.get('comentario'),
    )
    entidades = result.get('entities') or []
    return ok({
        'mensaje': (
            f'{len(entidades)} servicio(s) aplicado(s) al itinerario.'
            if len(entidades) != 1
            else 'Datos aprobados y aplicados al itinerario.'
        ),
        'extraccion': result['extraction'].to_dict(),
        'aplicaciones': [a.to_dict() for a in result['applications']],
    })


@api_v1_bp.route('/documents/<document_id>/reprocess', methods=['POST'])
@login_required
@require_document_access(Permiso.SUBIR_DOCUMENTO)
def reprocess_document(document_id, document):
    """Retry a failed pipeline from the last good state."""
    document_service.reprocess(current_user._get_current_object(), document)
    return ok({'mensaje': 'Reproceso iniciado.', 'estado': str(document.estado_proceso)})


@api_v1_bp.route('/documents/<document_id>', methods=['DELETE'])
@login_required
@require_document_access(Permiso.ELIMINAR_DOCUMENTO)
def delete_document(document_id, document):
    """Soft-delete a document."""
    document_service.delete(current_user._get_current_object(), document)
    return ok({'mensaje': 'Documento eliminado.'})
