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
from app.models.enums import AuditResourceType
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

    @property
    def por_dia(self):
        """Entries grouped by local calendar date, for the day-by-day view."""
        grouped = defaultdict(list)
        for entry in self.entries:
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


def find_connections(entries):
    """Pair consecutive transport segments of the same traveller.

    Returns a list of dicts carrying both segments, the margin in minutes
    computed in UTC, and whether the connection happens in the same place. The
    alert engine consumes the same structure, so the margin shown in the
    interface and the margin a rule judges are computed once, here.
    """
    connections = []
    by_traveler = defaultdict(list)

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

    if commit:
        db.session.commit()
        audit_service.record(
            f'itinerary.{kind}_created',
            recurso_tipo=_AUDIT_TYPES.get(kind),
            recurso_id=str(item.id),
            actor=actor,
            metadatos={'trip_id': str(trip.id)},
        )
    return item


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
    return item


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
