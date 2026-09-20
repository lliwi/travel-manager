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
    extraction.payload_normalizado = {'servicios': result.servicios}
    # Kept flat for the listing screens, which only need a rough figure: the
    # per-service confidences are inside each service.
    extraction.confianzas = {
        k: v
        for servicio in result.servicios
        for k, v in (servicio.get('confianzas') or {}).items()
    }
    extraction.confianza_global = result.confianza_global
    extraction.avisos = result.avisos
    extraction.schema_version = schema_version
    if commit:
        db.session.commit()
    return extraction


# ======================================================================
# The review screen
# ======================================================================
def servicios_de(extraction):
    """The services an extraction describes, whatever shape it was stored in.

    Extractions written before a document could describe more than one service
    are still in the database; they are read as a single service rather than
    migrated, because the payload is a record of what the model answered and
    rewriting it would falsify that.
    """
    datos = extraction.datos or {}

    servicios = datos.get('servicios')
    if isinstance(servicios, list) and servicios:
        return servicios

    campos = datos.get('campos')
    if isinstance(campos, dict):
        return [{
            'campos': campos,
            'confianzas': extraction.confianzas or {},
            'procedencias': (extraction.payload or {}).get('procedencias') or {},
        }]

    return []


def review_fields(extraction):
    """Rows for the review screen, grouped by service.

    Specification section 2.3 requires the reviewer to see the confidence and
    the reference document or page for each extracted value; this is what
    builds that view. One group per service, because a return booking's two
    flights are reviewed and corrected independently.
    """
    threshold = _review_threshold()
    grupos = []

    for indice, servicio in enumerate(servicios_de(extraction)):
        campos = servicio.get('campos') or {}
        confianzas = servicio.get('confianzas') or {}
        procedencias = servicio.get('procedencias') or {}

        filas = []
        for name, value in sorted(campos.items()):
            if name.endswith('_location_id'):
                continue

            confianza = confianzas.get(name)
            confianza = float(confianza) if isinstance(confianza, (int, float)) else None
            procedencia = procedencias.get(name) or {}
            fragmento = procedencia.get('fragmento')

            filas.append({
                'campo': name,
                'indice': indice,
                # The form field name carries the service, so a correction to
                # the return flight cannot land on the outbound one.
                'nombre_form': f'campo__{indice}__{name}',
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

        grupos.append({
            'indice': indice,
            'titulo': _titulo_servicio(servicio, indice),
            'campos': filas,
        })

    return grupos


def _titulo_servicio(servicio, indice):
    """A heading a reviewer can tell one service from another by."""
    campos = servicio.get('campos') or {}

    numero = campos.get('numero_vuelo') or campos.get('numero_tren')
    origen = campos.get('origen_codigo') or campos.get('origen_ciudad')
    destino = campos.get('destino_codigo') or campos.get('destino_ciudad')
    if origen and destino:
        ruta = f'{origen} → {destino}'
        return f'{numero} {ruta}' if numero else ruta

    nombre = campos.get('nombre') or campos.get('proveedor')
    if nombre:
        return str(nombre)

    return f'Servicio {indice + 1}'


def parse_review_form(extraction, form):
    """Read the reviewer's corrections out of a submitted form.

    Returns ``{indice_servicio: {campo: valor}}`` containing only what actually
    changed, so an untouched field keeps the provenance the extraction gave it.
    """
    correcciones = {}

    for indice, servicio in enumerate(servicios_de(extraction)):
        campos = servicio.get('campos') or {}
        del_servicio = {}

        for name, original in campos.items():
            base = f'campo__{indice}__{name}'

            if isinstance(original, dict) and 'local' in original:
                local = form.get(f'{base}__local')
                zona = form.get(f'{base}__zona')
                if local is None and zona is None:
                    continue
                nuevo = {
                    'local': local or None,
                    'zona_horaria': zona or original.get('zona_horaria'),
                }
                if nuevo != original:
                    del_servicio[name] = nuevo
                continue

            if base not in form:
                continue
            nuevo = form.get(base) or None
            if nuevo != (str(original) if original is not None else None):
                del_servicio[name] = nuevo

        if del_servicio:
            correcciones[indice] = del_servicio

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
    if extraction.estado is ExtractionState.APROBADA and not correcciones:
        raise ConflictError(
            'Esta extracción ya se ha aplicado. Corrija algún campo para '
            'volver a aplicarla.'
        )

    if correcciones:
        # A correction is a new version, never an edit of the machine's answer.
        extraction = _apply_corrections(actor, extraction, correcciones)

    target_kind, model = TARGET_BY_CLASSIFICATION.get(
        document.clasificacion, ('servicio', OtherService)
    )

    servicios = servicios_de(extraction)
    applications = []
    entities = []

    for indice, servicio in enumerate(servicios):
        entity, outcome, applied_fields = _apply_to_itinerary(
            actor, document, extraction, target_kind, model, servicio, indice
        )

        applications.append(ExtractionApplication(
            extraction_id=extraction.id,
            entidad_tipo=target_kind,
            entidad_id=entity.id if entity is not None else None,
            resultado=outcome,
            campos_aplicados=applied_fields,
            motivo=(
                None if entity is not None
                else 'No había datos suficientes que aplicar.'
            ),
        ))
        if entity is not None:
            entities.append(entity)

    # A re-approval that found fewer services than the previous one leaves rows
    # describing something the document no longer says. They are withdrawn
    # rather than left behind, and the withdrawal is recorded.
    applications.extend(
        _retirar_sobrantes(
            actor, document, extraction, model, target_kind, len(servicios)
        )
    )

    for application in applications:
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

    for entity in entities:
        provenance_service.recalculate_rollup(entity, target_kind, commit=False)

    # A trip created from its documents has no dates of its own until the
    # services they describe are approved. Empty fields only; a date the
    # manager typed stands.
    from app.services import trip_service

    fechas = trip_service.sync_dates_from_itinerary(document.trip, commit=False)
    destinos = trip_service.sync_destinations_from_itinerary(
        actor, document.trip, commit=False,
    )
    if destinos:
        logger.info(
            'Destinos deducidos del itinerario del viaje %s: %s',
            document.trip_id, ', '.join(d.ciudad or d.pais_codigo for d in destinos),
        )

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
            'servicios': len(servicios),
            'entidades': [str(e.id) for e in entities],
            'fechas_del_viaje_deducidas': fechas or None,
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
        'applications': applications,
        'entities': entities,
        # Kept for callers that only ever expected one.
        'entity': entities[0] if entities else None,
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
    """Create version N+1 carrying the reviewer's corrections plus a diff.

    ``correcciones`` is keyed by service index, so a correction to the return
    flight cannot land on the outbound one.
    """
    servicios = [
        {
            'campos': dict(s.get('campos') or {}),
            'confianzas': dict(s.get('confianzas') or {}),
            'procedencias': dict(s.get('procedencias') or {}),
        }
        for s in servicios_de(extraction)
    ]

    diff = {}
    for indice, cambios in (correcciones or {}).items():
        if indice >= len(servicios):
            continue
        servicio = servicios[indice]

        for name, nuevo in cambios.items():
            anterior = servicio['campos'].get(name)
            if anterior == nuevo:
                continue
            diff.setdefault(str(indice), {})[name] = {
                'antes': anterior, 'despues': nuevo,
            }
            servicio['campos'][name] = nuevo
            # A person typed it, and it will be recorded with origin 'manual',
            # which is what makes that certainty auditable.
            servicio['confianzas'][name] = 1.0
            servicio['procedencias'][name] = {'pagina': None, 'fragmento': None}

    nueva = create_version(
        extraction.document,
        payload={'servicios': servicios},
        metodo=ExtractionMethod.MANUAL,
        proveedor=extraction.proveedor,
        modelo=extraction.modelo,
        ai_run_id=extraction.ai_run_id,
        confianzas=extraction.confianzas,
        confianza_global=extraction.confianza_global,
        avisos=extraction.avisos,
        estado=ExtractionState.EN_REVISION,
        version_previa=extraction,
        diff_previa=diff,
        commit=False,
    )
    nueva.payload_normalizado = {'servicios': servicios}
    db.session.flush()
    return nueva


def _retirar_sobrantes(actor, document, extraction, model, kind, cuantos):
    """Withdraw entities from services a later extraction no longer describes."""
    sobrantes = model.query.filter(
        model.documento_origen_id == document.id,
        model.is_deleted.is_(False),
        model.documento_origen_indice.isnot(None),
        model.documento_origen_indice >= cuantos,
    ).all()

    aplicaciones = []
    for entity in sobrantes:
        entity.soft_delete(actor)
        aplicaciones.append(ExtractionApplication(
            # The withdrawal belongs to the approval that caused it, so it is
            # findable from the extraction rather than floating unattached.
            extraction_id=extraction.id,
            entidad_tipo=kind,
            entidad_id=entity.id,
            resultado=ApplicationOutcome.IGNORADA,
            motivo=(
                'La nueva extracción no describe este servicio; se retira del '
                'itinerario.'
            ),
        ))
    return aplicaciones


def _apply_to_itinerary(actor, document, extraction, kind, model, servicio, indice):
    """Write one service's approved fields onto an itinerary entity.

    Matched by ``(documento_origen_id, documento_origen_indice)``: re-approving
    updates the row this service created before rather than adding a second
    one, and the index is what keeps a return booking's two flights apart.
    """
    campos = (servicio.get('campos') or {}) if isinstance(servicio, dict) else {}
    if not campos:
        return None, ApplicationOutcome.IGNORADA, []

    mapping = FIELD_MAPPINGS.get(kind, {})
    instants = INSTANT_MAPPINGS.get(kind, {})

    existing = model.query.filter_by(
        trip_id=document.trip_id,
        documento_origen_id=document.id,
        documento_origen_indice=indice,
        is_deleted=False,
    ).first()

    entity = existing or model(
        trip_id=document.trip_id,
        trip_traveler_id=document.trip_traveler_id,
        documento_origen_id=document.id,
        documento_origen_indice=indice,
    )
    outcome = ApplicationOutcome.ACTUALIZADA if existing else ApplicationOutcome.CREADA

    _set_required_defaults(entity, kind, document, campos)

    # Flush before recording provenance: a new entity has no id until it is
    # flushed, and every provenance row references it.
    if existing is None:
        db.session.add(entity)
    db.session.flush()

    applied = []
    confianzas = servicio.get('confianzas') or {}
    procedencias = servicio.get('procedencias') or {}

    for source, target in mapping.items():
        if source not in campos:
            continue
        value = campos[source]
        if value is None or not hasattr(entity, target):
            continue

        setattr(entity, target, value)
        applied.append(target)

        procedencia = procedencias.get(source) or {}
        provenance_service.record_extracted_field(
            entity, kind, target, value, extraction,
            confianza=confianzas.get(source),
            pagina=procedencia.get('pagina'),
            fragmento=procedencia.get('fragmento'),
            actor=actor,
        )

    # A list of people with their seats has no column of its own: it belongs to
    # the segment, varies per leg, and a table for it would be a schema change
    # for something only some bookings carry. It goes in the entity's own JSON,
    # which is exactly what that column is for.
    pasajeros = campos.get('pasajeros')
    if isinstance(pasajeros, list) and pasajeros and hasattr(entity, 'datos'):
        limpios = [
            {clave: p.get(clave) for clave in ('nombre', 'asiento', 'equipaje')}
            for p in pasajeros if isinstance(p, dict) and p.get('nombre')
        ]
        if limpios:
            entity.datos = {**(entity.datos or {}), 'pasajeros': limpios}
            applied.append('pasajeros')
            provenance_service.record_extracted_field(
                entity, kind, 'pasajeros', limpios, extraction,
                confianza=(servicio.get('confianzas') or {}).get('pasajeros'),
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
            confianza=confianzas.get(source),
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


def confianza_minima(extraction):
    """The confidence of this extraction's weakest field, or None.

    The aggregate that decides whether a human has to look. A mean lets one
    inferred field hide behind several copied ones -- four fields at 0.85 and
    one at 0.5 average 0.78, and the 0.5 is exactly the one worth reading. The
    minimum cannot be averaged away.

    None when no field carries a confidence at all, which is not an invitation
    to assume the best: a caller deciding whether to skip review must treat the
    absence of evidence as a reason to ask.
    """
    valores = []
    for servicio in servicios_de(extraction):
        campos = servicio.get('campos') or {}
        for nombre, valor in (servicio.get('confianzas') or {}).items():
            # A field the document does not mention is absent, not unreliable.
            # Counting it would mean no real booking ever qualifies: none of
            # them fills every optional field.
            if campos.get(nombre) in (None, '', {}, []):
                continue
            if isinstance(valor, (int, float)):
                valores.append(float(valor))
    return min(valores) if valores else None


def puede_aprobarse_sola(extraction):
    """Whether this extraction goes to the itinerary without waiting.

    When the setting is on, it always does. That is a deliberate inversion of
    "a human confirms before data becomes real": the data lands, and the
    correction happens afterwards on the review screen, which stays reachable
    for exactly that. What the confidence buys is no longer a gate but a mark
    -- a field below the review threshold flags its entity as
    ``requiere_revision``, so the timeline shows what to look at instead of
    holding everything back until someone looks at all of it.

    The trade-off is stated rather than hidden: a value the model invented now
    reaches the itinerary. It arrives flagged, attributable to its document,
    and correctable, which is the bargain an organisation makes when it turns
    this on.

    Returns ``(puede, motivo)``; the reason is logged.
    """
    from app.services import settings_service

    if not settings_service.get_bool('DOCUMENTOS_AUTO_APROBAR', False):
        return False, 'la aprobación automática está desactivada'

    if extraction is None:
        return False, 'no hay extracción'

    if extraction.estado is ExtractionState.APROBADA:
        return False, 'ya estaba aprobada'

    minima = confianza_minima(extraction)
    detalle = (
        f'el campo menos fiable está en {minima:.2f}' if minima is not None
        else 'ningún campo lleva confianza'
    )
    return True, f'aprobación automática ({detalle})'


def _review_threshold():
    from app.services import settings_service

    return settings_service.get_float('DOCUMENTOS_UMBRAL_REVISION', 0.85)
