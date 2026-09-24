"""Itinerary consolidation.

Specification section 2.4: "El sistema consolidará todos los servicios en una
línea temporal por viajero y viaje." Everything is ordered by the UTC instant and
rendered in its own local time, and every query passes through
``scope_itinerary_query`` so a traveller never sees another traveller's rows.
"""
import logging
from collections import defaultdict
from datetime import UTC, datetime

from app.extensions import db
from app.models.enums import AuditResourceType, ProvenanceOrigin
from app.models.itinerary import (
    Accommodation,
    OtherService,
    TravelSegment,
    VehicleRental,
)
from app.services import audit_service
from app.services.authorization_service import scope_itinerary_query
from app.utils.errors import ResourceNotFound, ValidationError
from app.utils.timeutil import local_date, minutes_between, set_instant

logger = logging.getLogger(__name__)

#: Sort key for items with no start instant, so they land at the end of the
#: timeline instead of disappearing from it.
_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=UTC)

#: The four itinerary entity types, with the prefix naming their start instant.
ITEM_TYPES = (
    ('segmento', TravelSegment, 'salida', 'llegada'),
    ('alojamiento', Accommodation, 'check_in', 'check_out'),
    ('vehiculo', VehicleRental, 'recogida', 'devolucion'),
    ('servicio', OtherService, 'inicio', 'fin'),
)


class TimelineEntry:
    """One item on the consolidated timeline."""

    __slots__ = ('kind', 'item', 'inicio_utc', 'fin_utc', 'traveler')

    def __init__(self, kind, item, inicio_utc, fin_utc, traveler=None):
        self.kind = kind
        self.item = item
        self.inicio_utc = inicio_utc
        self.fin_utc = fin_utc
        self.traveler = traveler

    @property
    def id(self):
        return self.item.id

    @property
    def etiqueta(self):
        return getattr(self.item, 'etiqueta', str(self.item))

    @property
    def sin_fecha(self):
        """True for items with no start instant, which sort to the end."""
        return self.inicio_utc is None

    def __repr__(self):
        return f'<TimelineEntry {self.kind} {self.inicio_utc}>'


class Timeline:
    """The consolidated itinerary of one trip, as one actor may see it."""

    def __init__(self, trip, entries, connections=None, per_traveler=None):
        self.trip = trip
        self.entries = entries
        #: Consecutive transport pairs with the computed margin between them.
        self.connections = connections or []
        #: ``{trip_traveler_id or None: [entries]}``.
        self.per_traveler = per_traveler or {}

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    @property
    def is_empty(self):
        return not self.entries

    #: Kinds that are a journey rather than a stay. A stay spans nights and a
    #: journey happens at an instant, so putting both on one chronology means
    #: the hotel appears as a point between two flights and its three nights
    #: are nowhere.
    DESPLAZAMIENTOS = ('segmento', 'vehiculo', 'servicio')

    @property
    def desplazamientos(self):
        """Entries that are a movement, in order."""
        return [e for e in self.entries if e.kind in self.DESPLAZAMIENTOS]

    @property
    def estancias(self):
        """Entries that are a stay, in order."""
        return [e for e in self.entries if e.kind == 'alojamiento']

    def por_dia_de(self, entradas):
        """Group a subset of entries by local date, as :attr:`por_dia` does.

        Takes the subset rather than filtering inside, so the caller decides
        what belongs on a chronology and this stays a grouping function.
        """
        return self._agrupar(entradas)

    @property
    def por_dia(self):
        """Entries grouped by local calendar date, for the day-by-day view."""
        return self._agrupar(self.entries)

    def _agrupar(self, entradas):
        grouped = defaultdict(list)
        for entry in entradas:
            tz = _timezone_of(entry)
            day = local_date(entry.inicio_utc, tz) if entry.inicio_utc else None
            grouped[day].append(entry)
        # Dated days first in order; undated items last.
        ordered = sorted(d for d in grouped if d is not None)
        result = [(day, grouped[day]) for day in ordered]
        if None in grouped:
            result.append((None, grouped[None]))
        return result

    def to_dict(self, include_costs=False):
        return {
            'trip_id': str(self.trip.id),
            'itinerary_version': self.trip.itinerary_version,
            'entradas': [
                {
                    'tipo': entry.kind,
                    'inicio_utc': entry.inicio_utc.isoformat() if entry.inicio_utc else None,
                    'fin_utc': entry.fin_utc.isoformat() if entry.fin_utc else None,
                    'datos': entry.item.to_dict(include_costs=include_costs),
                }
                for entry in self.entries
            ],
            'conexiones': [
                {
                    'desde': str(c['desde'].id),
                    'hasta': str(c['hasta'].id),
                    'margen_minutos': c['margen_minutos'],
                    'mismo_lugar': c['mismo_lugar'],
                }
                for c in self.connections
            ],
        }


def build_timeline(actor, trip, traveler=None):
    """Build the consolidated timeline of a trip for one actor.

    Args:
        actor: Who is asking. Determines which rows are visible.
        trip: The trip.
        traveler: Optionally narrow to a single :class:`TripTraveler`.

    Returns:
        A :class:`Timeline`.
    """
    entries = []
    by_traveler = defaultdict(list)

    for kind, model, start_prefix, end_prefix in ITEM_TYPES:
        query = model.query.filter(
            model.trip_id == trip.id,
            model.is_deleted.is_(False),
        )
        query = scope_itinerary_query(actor, trip, query, model)
        if traveler is not None:
            query = query.filter(
                db.or_(
                    model.trip_traveler_id.is_(None),
                    model.trip_traveler_id == traveler.id,
                )
            )

        for item in query.all():
            entry = TimelineEntry(
                kind=kind,
                item=item,
                inicio_utc=getattr(item, f'{start_prefix}_utc', None),
                fin_utc=getattr(item, f'{end_prefix}_utc', None),
                traveler=item.traveler,
            )
            entries.append(entry)
            by_traveler[item.trip_traveler_id].append(entry)

    # Sort by UTC instant; undated items go last so they stay visible rather
    # than silently dropping out of the itinerary.
    entries.sort(key=lambda e: (e.inicio_utc is None, e.inicio_utc or _FAR_FUTURE))
    for group in by_traveler.values():
        group.sort(key=lambda e: (e.inicio_utc is None, e.inicio_utc or _FAR_FUTURE))

    return Timeline(
        trip=trip,
        entries=entries,
        connections=find_connections(entries),
        per_traveler=dict(by_traveler),
    )


#: Beyond this, two consecutive legs are not a connection. Used when no
#: administrator has set ``CONEXION_MAX_HORAS``.
CONEXION_MAX_HORAS_POR_DEFECTO = 24


def conexion_max_minutos():
    """How far apart two legs may be and still be one connection."""
    from app.services import settings_service

    horas = settings_service.get_int(
        'CONEXION_MAX_HORAS', CONEXION_MAX_HORAS_POR_DEFECTO
    )
    return max(1, horas) * 60


def es_conexion(margen_minutos, maximo=None):
    """Whether a gap between two consecutive legs is a connection at all.

    A connection is a transfer the traveller has to make: they land and the
    clock is running. Two legs days apart are the outbound and the return of a
    journey, with a stay in between -- reporting that as a connection of «58 h
    35 min» describes the trip as a transfer nobody has to catch.

    A negative margin means the legs overlap, which is the overlap rule's
    finding, not a connection.
    """
    if margen_minutos is None or margen_minutos < 0:
        return False
    return margen_minutos <= (maximo if maximo is not None else conexion_max_minutos())


def find_connections(entries):
    """Pair consecutive transport segments of the same traveller.

    Returns a list of dicts carrying both segments, the margin in minutes
    computed in UTC, and whether the connection happens in the same place.

    What counts as a connection is decided by :func:`es_conexion`, which the
    alert rule uses too: the interface and the rule must agree on which pairs
    are connections, or one of them is describing something the other does not
    recognise.
    """
    connections = []
    by_traveler = defaultdict(list)
    maximo = conexion_max_minutos()

    for entry in entries:
        if entry.kind != 'segmento':
            continue
        if entry.fin_utc is None:
            continue
        by_traveler[entry.item.trip_traveler_id].append(entry)

    for traveler_id, group in by_traveler.items():
        group.sort(key=lambda e: e.inicio_utc or _FAR_FUTURE)
        # Consecutive pairs; the offset slice is shorter by one.
        for first, second in zip(group, group[1:], strict=False):
            if second.inicio_utc is None or first.fin_utc is None:
                continue
            margin = minutes_between(first.fin_utc, second.inicio_utc)
            if not es_conexion(margin, maximo):
                continue
            connections.append({
                'trip_traveler_id': traveler_id,
                'desde': first.item,
                'hasta': second.item,
                'margen_minutos': margin,
                'mismo_lugar': _same_place(first.item, second.item),
            })

    return connections


def _same_place(first, second):
    """True when the second segment departs from where the first arrived."""
    if first.destino_codigo and second.origen_codigo:
        return first.destino_codigo.upper() == second.origen_codigo.upper()
    if first.destino_ciudad and second.origen_ciudad:
        return first.destino_ciudad.strip().lower() == second.origen_ciudad.strip().lower()
    return False


def _timezone_of(entry):
    """The timezone an entry should be rendered in."""
    for attr in ('salida_tz', 'check_in_tz', 'recogida_tz', 'inicio_tz'):
        value = getattr(entry.item, attr, None)
        if value:
            return value
    return 'UTC'


# ======================================================================
# Manual CRUD
# ======================================================================
#: Maps the API/form entity name to its model and instant prefixes.
ENTITY_MAP = {
    'segmento': (TravelSegment, ('salida', 'llegada')),
    'alojamiento': (Accommodation, ('check_in', 'check_out')),
    'vehiculo': (VehicleRental, ('recogida', 'devolucion')),
    'servicio': (OtherService, ('inicio', 'fin')),
}

_AUDIT_TYPES = {
    'segmento': AuditResourceType.SEGMENTO,
    'alojamiento': AuditResourceType.ALOJAMIENTO,
    'vehiculo': AuditResourceType.VEHICULO,
    'servicio': AuditResourceType.SERVICIO,
}


def create_item(actor, trip, kind, instants=None, commit=True, **campos):
    """Create an itinerary item by hand.

    Args:
        kind: One of ``segmento``, ``alojamiento``, ``vehiculo``, ``servicio``.
        instants: ``{prefix: (local_dt, tz_name)}`` for the timestamp triples.
        campos: Plain column values.

    Every field written here gets provenance ``manual`` -- a person typed it.
    """
    model, prefixes = _resolve_kind(kind)

    item = model(trip_id=trip.id)
    for field, value in campos.items():
        if hasattr(item, field):
            setattr(item, field, value)

    for prefix, (local_dt, tz_name) in (instants or {}).items():
        if prefix not in prefixes:
            raise ValidationError(f'El campo temporal «{prefix}» no existe en {kind}.')
        set_instant(item, prefix, local_dt, tz_name or trip.inicio_tz)

    db.session.add(item)
    db.session.flush()

    from app.services import provenance_service

    provenance_service.record_manual_fields(
        item, kind, campos.keys(), actor, commit=False
    )
    trip.touch_itinerary()
    # Escribir un tramo es decir que esto ya no es un boceto. Pedir además que
    # alguien se acuerde de mover el estado es como una lista se llena de
    # borradores que en realidad están en marcha.
    from app.services import trip_service

    trip_service.marcar_actividad(trip, actor, commit=False)

    if commit:
        db.session.commit()
        audit_service.record(
            f'itinerary.{kind}_created',
            recurso_tipo=_AUDIT_TYPES.get(kind),
            recurso_id=str(item.id),
            actor=actor,
            metadatos={'trip_id': str(trip.id)},
        )
        _schedule_alert_recalculation(trip, actor)
    return item


def pendientes_de_confirmar(actor, trip):
    """Every item of a trip with fields the extraction was unsure about.

    Scoped through the timeline, so a traveller sees the fields of their own
    rows and nobody else's -- the same query the itinerary uses, rather than a
    second one that could disagree with it.

    Returns:
        ``[{'kind', 'item', 'campos': [(campo, FieldProvenance)]}]``.
    """
    from app.services import provenance_service

    umbral = provenance_service.umbral_revision()
    pendientes = []

    for entrada in build_timeline(actor, trip).entries:
        if not entrada.item.requiere_revision:
            continue

        actual = provenance_service.current_for(entrada.item, entrada.kind)
        campos = sorted(
            (campo, fila) for campo, fila in actual.items()
            if fila.confianza is not None and float(fila.confianza) < umbral
        )
        if campos:
            pendientes.append({
                'kind': entrada.kind, 'item': entrada.item, 'campos': campos,
            })

    return pendientes


def confirm_items(actor, trip, seleccion):
    """Confirm several items at once.

    ``seleccion`` is ``[(kind, item_id)]``. Committed once at the end rather
    than per item: confirming twelve rows is one decision a person made, and
    half of it applied is a worse state than none of it.

    Returns:
        ``(elementos_confirmados, campos_confirmados)``.
    """
    elementos = 0
    campos = 0

    for kind, item_id in seleccion:
        confirmados = confirm_item(actor, trip, kind, item_id, commit=False)
        if confirmados:
            elementos += 1
            campos += len(confirmados)

    if elementos:
        db.session.commit()
        audit_service.record(
            'itinerary.confirmed_batch',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'elementos': elementos, 'campos': campos},
        )
    return elementos, campos


def confirm_item(actor, trip, kind, item_id, commit=True):
    """Mark an item's doubtful fields as checked by a person.

    The missing half of «requiere revisión». Until now the only way to clear
    the mark was to *change* a value, so an item the extraction read badly but
    got right stayed flagged for ever: there was no way to say «I looked, and
    it is correct».

    What it records is not «ignore this». It writes the same value back with
    manual provenance and full confidence, naming who confirmed it -- which is
    what the specification asks for when extracted data becomes real, and what
    makes «who said this was right» answerable afterwards.

    Returns:
        The list of field names that were confirmed.
    """
    from app.services import provenance_service

    model, _ = _resolve_kind(kind)
    item = _load_item(model, trip, item_id)

    umbral = provenance_service.umbral_revision()
    actual = provenance_service.current_for(item, kind)
    dudosos = [
        campo for campo, fila in actual.items()
        if fila.confianza is not None and float(fila.confianza) < umbral
    ]

    if not dudosos:
        return []

    # Escrito aquí y no con «record_manual_fields», que se salta los campos
    # que no son atributos del modelo. La extracción registra procedencia de
    # cosas que viven dentro de «datos» —los pasajeros, el nombre del
    # aeropuerto—, así que confirmarlas no hacía nada: el elemento seguía
    # marcado, sin error y sin explicación. El valor se toma de la propia
    # fila de procedencia, que es quien lo sabe.
    for campo in dudosos:
        provenance_service.record(
            item, kind, campo,
            origen=ProvenanceOrigin.MANUAL,
            valor_actual=actual[campo].valor_actual,
            confianza=1.0,
            actor=actor,
        )

    provenance_service.recalculate_rollup(item, kind, commit=False)
    trip.touch_itinerary()

    if commit:
        db.session.commit()
        audit_service.record(
            f'itinerary.{kind}_confirmed',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'item_id': str(item.id), 'campos': sorted(dudosos)},
        )
    return dudosos


def update_item(actor, trip, kind, item_id, instants=None, commit=True, **campos):
    """Edit an itinerary item by hand."""
    model, prefixes = _resolve_kind(kind)
    item = _load_item(model, trip, item_id)

    cambiados = []
    for field, value in campos.items():
        if not hasattr(item, field):
            continue
        anterior = getattr(item, field)
        if anterior != value:
            from app.services import provenance_service

            provenance_service.record_manual_change(
                item, kind, field, anterior, value, actor, commit=False
            )
            setattr(item, field, value)
            cambiados.append(field)

    for prefix, (local_dt, tz_name) in (instants or {}).items():
        if prefix not in prefixes:
            raise ValidationError(f'El campo temporal «{prefix}» no existe en {kind}.')
        anterior = getattr(item, f'{prefix}_local', None)
        set_instant(item, prefix, local_dt, tz_name or getattr(item, f'{prefix}_tz', None))
        if anterior != getattr(item, f'{prefix}_local', None):
            from app.services import provenance_service

            provenance_service.record_manual_change(
                item, kind, f'{prefix}_local', anterior,
                getattr(item, f'{prefix}_local'), actor, commit=False,
            )
            cambiados.append(prefix)

    if cambiados:
        # Sin esto, corregir el campo dudoso no quitaba la marca: la fila
        # seguía diciendo «sin confirmar» con todos sus datos ya revisados,
        # porque «requiere_revision» solo se recalculaba al aplicar una
        # extracción. Quien lo arreglaba lo veía igual que antes.
        from app.services import provenance_service

        provenance_service.recalculate_rollup(item, kind, commit=False)
        trip.touch_itinerary()

    if commit:
        db.session.commit()
        if cambiados:
            audit_service.record(
                f'itinerary.{kind}_updated',
                recurso_tipo=_AUDIT_TYPES.get(kind),
                recurso_id=str(item.id),
                actor=actor,
                metadatos={'campos': sorted(set(cambiados))},
            )
            _schedule_alert_recalculation(trip, actor)
    return item


def delete_item(actor, trip, kind, item_id, commit=True):
    """Soft-delete an itinerary item."""
    model, _ = _resolve_kind(kind)
    item = _load_item(model, trip, item_id)

    item.soft_delete(actor)
    trip.touch_itinerary()

    if commit:
        db.session.commit()
        audit_service.record(
            f'itinerary.{kind}_deleted',
            recurso_tipo=_AUDIT_TYPES.get(kind),
            recurso_id=str(item.id),
            actor=actor,
        )
        _schedule_alert_recalculation(trip, actor)
    return item


def _schedule_alert_recalculation(trip, actor):
    """Re-evaluate the trip's alerts after the itinerary changed by hand.

    A margin that no longer holds, or one that now does, is the whole point of
    the alert engine. Leaving this to the nightly pass meant a manager who
    corrected a departure time saw the old alert until the next morning.
    """
    from app.models.enums import AlertTrigger
    from app.services import alert_service

    alert_service.schedule_recalculation(
        trip.id, trigger=AlertTrigger.ITINERARIO, actor=actor
    )


def get_item(trip, kind, item_id):
    """One itinerary item of this trip, or 404.

    The edit form needs the row before it can show it, and it must be the same
    lookup the write paths use -- an item belonging to another trip is not
    found here either.
    """
    model, _ = _resolve_kind(kind)
    return _load_item(model, trip, item_id)


def _resolve_kind(kind):
    entry = ENTITY_MAP.get(str(kind))
    if entry is None:
        raise ValidationError(f'Tipo de elemento de itinerario desconocido: {kind}.')
    return entry


def _load_item(model, trip, item_id):
    import uuid

    try:
        item = db.session.get(model, uuid.UUID(str(item_id)))
    except (ValueError, TypeError):
        item = None
    if item is None or item.is_deleted or item.trip_id != trip.id:
        raise ResourceNotFound('El elemento de itinerario no existe en este viaje.')
    return item
