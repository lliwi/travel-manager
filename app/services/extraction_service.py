"""Extraction review and approval (specification section 2.3 steps 6-7, flow 5.2).

Approving an extraction is one transaction that does five things: it writes the
itinerary entities, records which fields came from where, records what the
approval created or changed, marks who confirmed it, and schedules an alert
recalculation.

A reviewer's correction never edits the payload in place. It produces version
N+1 with ``metodo='manual'`` and a field-level diff, so "se registra quién los
confirmó" holds for the machine's answer *and* for the human's correction.
"""
import logging

from app.extensions import db
from app.models.enums import (
    ApplicationOutcome,
    AuditResourceType,
    DocumentClassification,
    DocumentProcessState,
    ExtractionMethod,
    ExtractionState,
    ProvenanceOrigin,
    SegmentType,
    ServiceType,
)
from app.models.extraction import Extraction, ExtractionApplication
from app.models.itinerary import (
    Accommodation,
    OtherService,
    TravelSegment,
    VehicleRental,
)
from app.services import audit_service, document_service, provenance_service
from app.utils.errors import ConflictError
from app.utils.timeutil import set_instant, utcnow

logger = logging.getLogger(__name__)

#: Which itinerary entity each document class produces.
TARGET_BY_CLASSIFICATION = {
    DocumentClassification.VUELO: ('segmento', TravelSegment),
    DocumentClassification.TREN: ('segmento', TravelSegment),
    DocumentClassification.HOTEL: ('alojamiento', Accommodation),
    DocumentClassification.VEHICULO: ('vehiculo', VehicleRental),
    DocumentClassification.SEGURO: ('servicio', OtherService),
    DocumentClassification.VISADO: ('servicio', OtherService),
    DocumentClassification.OTRO: ('servicio', OtherService),
    DocumentClassification.DESCONOCIDO: ('servicio', OtherService),
}


# ======================================================================
# Creating versions
# ======================================================================
def create_version(document, payload, metodo=ExtractionMethod.IA, proveedor=None,
                   modelo=None, ai_run_id=None, confianzas=None,
                   confianza_global=None, avisos=None, estado=ExtractionState.PENDIENTE,
                   version_previa=None, diff_previa=None, commit=True):
    """Add a new extraction version, superseding the previous current one."""
    latest = (
        Extraction.query.filter_by(document_id=document.id)
        .order_by(Extraction.version.desc())
        .first()
    )
    next_version = (latest.version + 1) if latest else 1

    for previous in Extraction.query.filter_by(
        document_id=document.id, is_current=True
    ).all():
        previous.is_current = False
        if previous.estado in (ExtractionState.PENDIENTE, ExtractionState.EN_REVISION):
            previous.estado = ExtractionState.SUPERADA

    extraction = Extraction(
        document_id=document.id,
        version=next_version,
        is_current=True,
        estado=estado,
        metodo=metodo,
        payload=payload,
        confianzas=confianzas or {},
        confianza_global=confianza_global,
        avisos=avisos or [],
        proveedor=proveedor,
        modelo=modelo,
        ai_run_id=ai_run_id,
        version_previa_id=version_previa.id if version_previa else None,
        diff_previa=diff_previa,
    )
    db.session.add(extraction)

    if commit:
        db.session.commit()
    return extraction


def set_normalized(extraction, result, schema_version='1.0', commit=True):
    """Store the normalised payload alongside the raw one."""
    extraction.payload_normalizado = {'campos': result.campos}
    extraction.confianzas = result.confianzas
    extraction.confianza_global = result.confianza_global
    extraction.avisos = result.avisos
    extraction.schema_version = schema_version
    if commit:
        db.session.commit()
    return extraction


# ======================================================================
# The review screen
# ======================================================================
def review_fields(extraction):
    """Rows for the review screen: value, confidence, origin and source page.

    Specification section 2.3 requires the reviewer to see the confidence and the
    reference document or page for each extracted value; this is what builds
    that view.
    """
    datos = extraction.datos or {}
    campos = datos.get('campos', datos) if isinstance(datos, dict) else {}
    procedencias = (extraction.payload or {}).get('procedencias') or {}
    threshold = _review_threshold()

    rows = []
    for name, value in sorted(campos.items()):
        if name.endswith('_location_id'):
            continue

        confianza = extraction.confianza_de(name)
        procedencia = procedencias.get(name) or {}
        fragmento = procedencia.get('fragmento')

        rows.append({
            'campo': name,
            'etiqueta': _label_for(name),
            'valor': _display(value),
            'valor_bruto': value,
            'es_instante': isinstance(value, dict) and 'local' in value,
            'confianza': confianza,
            'confianza_pct': int(confianza * 100) if confianza is not None else None,
            'requiere_revision': confianza is None or confianza < threshold,
            'origen': (
                ProvenanceOrigin.DOCUMENTO if fragmento else ProvenanceOrigin.IA
            ),
            'pagina': procedencia.get('pagina'),
            'fragmento': fragmento,
        })
    return rows


def parse_review_form(extraction, form):
    """Read the reviewer's corrections out of a submitted form.

    Returns ``{campo: valor}`` containing only what the reviewer actually
    changed, so an untouched field keeps its machine provenance.
    """
    datos = extraction.datos or {}
    campos = datos.get('campos', datos) if isinstance(datos, dict) else {}
    correcciones = {}

    for name, original in campos.items():
        if isinstance(original, dict) and 'local' in original:
            local = form.get(f'campo__{name}__local')
            zona = form.get(f'campo__{name}__zona')
            if local is None and zona is None:
                continue
            nuevo = {
                'local': local or None,
                'zona_horaria': zona or original.get('zona_horaria'),
            }
            if nuevo != original:
                correcciones[name] = nuevo
            continue

        field = f'campo__{name}'
        if field not in form:
            continue
        nuevo = form.get(field)
        nuevo = nuevo if nuevo not in ('', None) else None
        if nuevo != (str(original) if original is not None else None):
            correcciones[name] = nuevo

    return correcciones


# ======================================================================
# Approval
# ======================================================================
def approve(actor, extraction, correcciones=None, comentario=None):
    """Approve an extraction and apply it to the itinerary (flow 5.2).

    All of it commits together: partially applied approvals would leave an
    itinerary that no provenance record explains.
    """
    document = extraction.document
    if extraction.estado is ExtractionState.APROBADA:
        raise ConflictError('Esta extracción ya ha sido aprobada.')

    if correcciones:
        # A correction is a new version, never an edit of the machine's answer.
        extraction = _apply_corrections(actor, extraction, correcciones)

    target_kind, model = TARGET_BY_CLASSIFICATION.get(
        document.clasificacion, ('servicio', OtherService)
    )

    entity, outcome, applied_fields = _apply_to_itinerary(
        actor, document, extraction, target_kind, model
    )

    application = ExtractionApplication(
        extraction_id=extraction.id,
        entidad_tipo=target_kind,
        entidad_id=entity.id if entity is not None else None,
        resultado=outcome,
        campos_aplicados=applied_fields,
        motivo=None if entity is not None else 'No había datos suficientes que aplicar.',
    )
    db.session.add(application)

    extraction.estado = ExtractionState.APROBADA
    extraction.aprobado_por_id = actor.id
    extraction.aprobado_en = utcnow()
    extraction.revisado_por_id = actor.id
    extraction.revisado_en = utcnow()
    extraction.comentario_revision = comentario

    document_service.transition(
        document, DocumentProcessState.APROBADO,
        tarea='extraction.approve', actor=actor, commit=False,
    )

    if entity is not None:
        provenance_service.recalculate_rollup(entity, target_kind, commit=False)

    document.trip.touch_itinerary()
    db.session.commit()

    audit_service.record(
        'extraction.approved',
        recurso_tipo=AuditResourceType.EXTRACCION,
        recurso_id=str(extraction.id),
        actor=actor,
        metadatos={
            'document_id': str(document.id),
            'trip_id': str(document.trip_id),
            'version': extraction.version,
            'entidad': target_kind,
            'entidad_id': str(entity.id) if entity else None,
            'resultado': str(outcome),
            'campos': applied_fields,
        },
    )

    # Recalculating after the commit, so the engine sees the new itinerary.
    from app.models.enums import AlertTrigger
    from app.services import alert_service

    alert_service.schedule_recalculation(
        document.trip_id, trigger=AlertTrigger.DOCUMENTO, actor=actor
    )

    return {
        'extraction': extraction,
        'applications': [application],
        'entity': entity,
    }


def reject(actor, extraction, comentario=None, commit=True):
    """Discard an extraction without applying it."""
    extraction.estado = ExtractionState.RECHAZADA
    extraction.revisado_por_id = actor.id
    extraction.revisado_en = utcnow()
    extraction.comentario_revision = comentario

    document_service.transition(
        extraction.document, DocumentProcessState.DESCARTADO,
        tarea='extraction.reject', actor=actor,
        motivo=comentario, commit=False,
    )

    if commit:
        db.session.commit()
        audit_service.record(
            'extraction.rejected',
            recurso_tipo=AuditResourceType.EXTRACCION,
            recurso_id=str(extraction.id),
            actor=actor,
            metadatos={
                'document_id': str(extraction.document_id),
                'motivo': comentario,
            },
        )
    return extraction


def _apply_corrections(actor, extraction, correcciones):
    """Create version N+1 carrying the reviewer's corrections plus a diff."""
    datos = dict(extraction.datos or {})
    campos = dict(datos.get('campos', {}))

    diff = {}
    for name, nuevo in correcciones.items():
        anterior = campos.get(name)
        if anterior != nuevo:
            diff[name] = {'antes': anterior, 'despues': nuevo}
            campos[name] = nuevo

    confianzas = dict(extraction.confianzas or {})
    # A human typed it, so it is certain -- and it will be recorded with origin
    # 'manual', which is what makes that certainty auditable.
    for name in diff:
        confianzas[name] = 1.0

    nueva = create_version(
        extraction.document,
        payload={'campos': campos, 'procedencias': (extraction.payload or {}).get('procedencias', {})},
        metodo=ExtractionMethod.MANUAL,
        proveedor=extraction.proveedor,
        modelo=extraction.modelo,
        ai_run_id=extraction.ai_run_id,
        confianzas=confianzas,
        confianza_global=extraction.confianza_global,
        avisos=extraction.avisos,
        estado=ExtractionState.EN_REVISION,
        version_previa=extraction,
        diff_previa=diff,
        commit=False,
    )
    nueva.payload_normalizado = {'campos': campos}
    db.session.flush()
    return nueva


def _apply_to_itinerary(actor, document, extraction, kind, model):
    """Write the approved fields onto an itinerary entity.

    Re-approving a document updates the entity it created before rather than
    creating a second one, which is what keeps a corrected booking from
    appearing twice on the timeline.
    """
    datos = extraction.datos or {}
    campos = datos.get('campos', {}) if isinstance(datos, dict) else {}
    if not campos:
        return None, ApplicationOutcome.IGNORADA, []

    mapping = FIELD_MAPPINGS.get(kind, {})
    instants = INSTANT_MAPPINGS.get(kind, {})

    existing = model.query.filter_by(
        trip_id=document.trip_id,
        documento_origen_id=document.id,
        is_deleted=False,
    ).first()

    entity = existing or model(
        trip_id=document.trip_id,
        trip_traveler_id=document.trip_traveler_id,
        documento_origen_id=document.id,
    )
    outcome = ApplicationOutcome.ACTUALIZADA if existing else ApplicationOutcome.CREADA

    _set_required_defaults(entity, kind, document, campos)

    # Flush before recording provenance: a new entity has no id until it is
    # flushed, and every provenance row references it.
    if existing is None:
        db.session.add(entity)
    db.session.flush()

    applied = []
    procedencias = (extraction.payload or {}).get('procedencias') or {}

    for source, target in mapping.items():
        if source not in campos:
            continue
        value = campos[source]
        if value is None:
            continue
        if not hasattr(entity, target):
            continue

        setattr(entity, target, value)
        applied.append(target)

        procedencia = procedencias.get(source) or {}
        provenance_service.record_extracted_field(
            entity, kind, target, value, extraction,
            confianza=extraction.confianza_de(source),
            pagina=procedencia.get('pagina'),
            fragmento=procedencia.get('fragmento'),
            actor=actor,
        )

    for source, prefix in instants.items():
        instant = campos.get(source)
        if not isinstance(instant, dict) or not instant.get('local'):
            continue
        local_dt = _parse_local(instant['local'])
        if local_dt is None:
            continue

        set_instant(entity, prefix, local_dt, instant.get('zona_horaria'))
        applied.append(prefix)

        procedencia = procedencias.get(source) or {}
        provenance_service.record_extracted_field(
            entity, kind, f'{prefix}_local', instant['local'], extraction,
            confianza=extraction.confianza_de(source),
            pagina=procedencia.get('pagina'),
            fragmento=procedencia.get('fragmento'),
            actor=actor,
        )

    db.session.flush()

    return entity, outcome, applied


def _set_required_defaults(entity, kind, document, campos):
    """Fill the non-nullable columns each entity type needs."""
    if kind == 'segmento':
        if getattr(entity, 'tipo', None) is None:
            entity.tipo = (
                SegmentType.TREN
                if document.clasificacion is DocumentClassification.TREN
                else SegmentType.VUELO
            )
    elif kind == 'alojamiento':
        if not getattr(entity, 'nombre', None):
            entity.nombre = campos.get('nombre') or 'Alojamiento sin nombre'
    elif kind == 'servicio':
        if getattr(entity, 'tipo', None) is None:
            entity.tipo = {
                DocumentClassification.SEGURO: ServiceType.SEGURO,
                DocumentClassification.VISADO: ServiceType.VISADO,
            }.get(document.clasificacion, ServiceType.OTRO)
        if not getattr(entity, 'nombre', None):
            entity.nombre = (
                campos.get('nombre')
                or campos.get('tipo_servicio')
                or document.nombre_original[:250]
            )


def _parse_local(raw):
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(raw))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError):
        return None


#: Extracted field -> entity column, per entity kind.
FIELD_MAPPINGS = {
    'segmento': {
        'localizador': 'localizador',
        'aerolinea': 'proveedor',
        'operador': 'proveedor',
        'numero_vuelo': 'numero',
        'numero_tren': 'numero',
        'operado_por': 'operado_por',
        'clase': 'clase',
        'asiento': 'asiento',
        'origen_codigo': 'origen_codigo',
        'origen_nombre': 'origen_nombre',
        'origen_ciudad': 'origen_ciudad',
        'origen_pais': 'origen_pais',
        'terminal_origen': 'terminal_origen',
        'destino_codigo': 'destino_codigo',
        'destino_nombre': 'destino_nombre',
        'destino_ciudad': 'destino_ciudad',
        'destino_pais': 'destino_pais',
        'terminal_destino': 'terminal_destino',
        'importe': 'importe',
        'moneda': 'moneda',
    },
    'alojamiento': {
        'localizador': 'localizador',
        'nombre': 'nombre',
        'cadena': 'proveedor',
        'direccion': 'direccion',
        'ciudad': 'ciudad',
        'pais': 'pais',
        'telefono': 'telefono',
        'numero_habitaciones': 'numero_habitaciones',
        'tipo_habitacion': 'tipo_habitacion',
        'regimen': 'regimen',
        'importe': 'importe',
        'moneda': 'moneda',
    },
    'vehiculo': {
        'localizador': 'localizador',
        'proveedor': 'proveedor',
        'categoria': 'categoria',
        'modelo': 'modelo',
        'transmision': 'transmision',
        'conductor': 'conductor_nombre',
        'recogida_lugar': 'recogida_lugar',
        'recogida_ciudad': 'recogida_ciudad',
        'recogida_pais': 'recogida_pais',
        'devolucion_lugar': 'devolucion_lugar',
        'devolucion_ciudad': 'devolucion_ciudad',
        'devolucion_pais': 'devolucion_pais',
        'franquicia': 'franquicia',
        'importe': 'importe',
        'moneda': 'moneda',
    },
    'servicio': {
        'localizador': 'localizador',
        'nombre': 'nombre',
        'aseguradora': 'proveedor',
        'proveedor': 'proveedor',
        'numero_poliza': 'localizador',
        'lugar': 'lugar',
        'ciudad': 'ciudad',
        'pais': 'pais',
        'importe': 'importe',
        'moneda': 'moneda',
    },
}

#: Extracted instant -> entity column prefix, per entity kind.
INSTANT_MAPPINGS = {
    'segmento': {'salida': 'salida', 'llegada': 'llegada'},
    'alojamiento': {'check_in': 'check_in', 'check_out': 'check_out'},
    'vehiculo': {'recogida': 'recogida', 'devolucion': 'devolucion'},
    'servicio': {'inicio': 'inicio', 'fin': 'fin'},
}

#: Spanish labels for the review screen.
_LABELS = {
    'localizador': 'Localizador', 'aerolinea': 'Aerolínea', 'operador': 'Operador',
    'numero_vuelo': 'Número de vuelo', 'numero_tren': 'Número de tren',
    'operado_por': 'Operado por', 'pasajero': 'Pasajero', 'clase': 'Clase',
    'asiento': 'Asiento', 'coche': 'Coche',
    'origen_codigo': 'Código de origen', 'origen_nombre': 'Origen',
    'origen_ciudad': 'Ciudad de origen', 'terminal_origen': 'Terminal de origen',
    'destino_codigo': 'Código de destino', 'destino_nombre': 'Destino',
    'destino_ciudad': 'Ciudad de destino', 'terminal_destino': 'Terminal de destino',
    'salida': 'Salida', 'llegada': 'Llegada',
    'nombre': 'Nombre', 'cadena': 'Cadena', 'direccion': 'Dirección',
    'ciudad': 'Ciudad', 'pais': 'País', 'telefono': 'Teléfono',
    'huesped': 'Huésped', 'check_in': 'Entrada', 'check_out': 'Salida',
    'noches': 'Noches', 'numero_habitaciones': 'Habitaciones',
    'tipo_habitacion': 'Tipo de habitación', 'regimen': 'Régimen',
    'proveedor': 'Proveedor', 'categoria': 'Categoría', 'modelo': 'Modelo',
    'transmision': 'Transmisión', 'conductor': 'Conductor',
    'recogida_lugar': 'Lugar de recogida', 'recogida': 'Recogida',
    'devolucion_lugar': 'Lugar de devolución', 'devolucion': 'Devolución',
    'franquicia': 'Franquicia', 'importe': 'Importe', 'moneda': 'Moneda',
    'aseguradora': 'Aseguradora', 'numero_poliza': 'Número de póliza',
    'asegurado': 'Asegurado', 'cobertura': 'Cobertura',
    'telefono_asistencia': 'Teléfono de asistencia',
    'inicio': 'Inicio', 'fin': 'Fin',
    'numero': 'Número', 'tipo': 'Tipo', 'titular': 'Titular',
    'pais_emisor': 'País emisor', 'fecha_emision': 'Fecha de emisión',
    'fecha_caducidad': 'Fecha de caducidad',
}


def _label_for(name):
    return _LABELS.get(name, name.replace('_', ' ').capitalize())


def _display(value):
    """Render a value for the review screen."""
    if value is None:
        return ''
    if isinstance(value, dict) and 'local' in value:
        local = value.get('local') or ''
        zone = value.get('zona_horaria')
        return f'{local} ({zone})' if zone else local
    return str(value)


def _review_threshold():
    from app.services import settings_service

    return settings_service.get_float('DOCUMENTOS_UMBRAL_REVISION', 0.85)
