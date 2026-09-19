"""Trip creation, editing, traveller assignment and destinations.

Specification section 2.2 and flow 5.1. Authorisation is *not* done here --
callers come through ``authorization_service`` first, which keeps a single
enforcement point for both surfaces.
"""
import logging
import re
from datetime import datetime

from sqlalchemy import func

from app.extensions import db
from app.models.enums import AuditResourceType, TripStatus
from app.models.trip import Trip, TripDestination, TripTraveler
from app.models.user import User
from app.services import audit_service
from app.utils.errors import ConflictError, ResourceNotFound, ValidationError
from app.utils.timeutil import set_instant, utcnow

logger = logging.getLogger(__name__)

#: Reference format: VJ-2026-0001.
REFERENCE_PREFIX = 'VJ'
_REFERENCE_RE = re.compile(r'^VJ-(\d{4})-(\d{4,})$')


def generate_reference(year=None):
    """Allocate the next internal reference for a trip.

    Sequential per calendar year. Races are resolved by the unique constraint on
    ``trips.referencia``; the caller retries, which is cheaper than a lock.
    """
    year = year or utcnow().year
    prefix = f'{REFERENCE_PREFIX}-{year}-'

    last = (
        db.session.query(func.max(Trip.referencia))
        .filter(Trip.referencia.like(f'{prefix}%'))
        .scalar()
    )
    if last:
        match = _REFERENCE_RE.match(last)
        nxt = int(match.group(2)) + 1 if match else 1
    else:
        nxt = 1
    return f'{prefix}{nxt:04d}'


def create_trip(actor, titulo, gestor=None, estado=TripStatus.BORRADOR,
                finalidad=None, finalidad_detalle=None, observaciones=None,
                inicio_local=None, inicio_tz=None, fin_local=None, fin_tz=None,
                referencia=None, commit=True):
    """Create a trip.

    The manager defaults to the creating actor, which is the common case: a
    manager opening a trip they will run themselves.
    """
    if not titulo or not str(titulo).strip():
        raise ValidationError('El título del viaje es obligatorio.')

    trip = Trip(
        referencia=referencia or generate_reference(),
        titulo=str(titulo).strip()[:300],
        estado=estado,
        finalidad=finalidad,
        finalidad_detalle=finalidad_detalle,
        observaciones=observaciones,
        gestor_id=(gestor.id if gestor is not None else actor.id),
        creado_por_id=actor.id,
        organizacion_id=getattr(actor, 'organizacion_id', None),
    )

    default_tz = _default_timezone(actor)
    set_instant(trip, 'inicio', inicio_local, inicio_tz or default_tz)
    set_instant(trip, 'fin', fin_local, fin_tz or inicio_tz or default_tz)
    _validate_dates(trip)

    db.session.add(trip)

    if commit:
        db.session.commit()
        audit_service.record(
            'trip.created',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'referencia': trip.referencia, 'titulo': trip.titulo},
        )
    return trip


def update_trip(actor, trip, commit=True, **campos):
    """Apply field changes to a trip, recording what actually changed."""
    if not trip.is_editable:
        raise ConflictError('Un viaje cancelado no puede modificarse.')

    cambios = {}
    simple_fields = (
        'titulo', 'finalidad', 'finalidad_detalle', 'observaciones', 'estado',
        'gestor_id', 'coste_estimado', 'moneda',
    )
    for field in simple_fields:
        if field not in campos:
            continue
        nuevo = campos[field]
        actual = getattr(trip, field)
        if actual != nuevo:
            cambios[field] = {'antes': _readable(actual), 'despues': _readable(nuevo)}
            setattr(trip, field, nuevo)

    # Dates come as (local, tz) pairs and go through set_instant so the UTC
    # column can never drift from the local one.
    for prefix in ('inicio', 'fin'):
        local_key, tz_key = f'{prefix}_local', f'{prefix}_tz'
        if local_key in campos:
            antes = getattr(trip, local_key)
            set_instant(
                trip, prefix, campos[local_key],
                campos.get(tz_key) or getattr(trip, tz_key) or _default_timezone(actor),
            )
            if antes != getattr(trip, local_key):
                cambios[prefix] = {
                    'antes': _readable(antes),
                    'despues': _readable(getattr(trip, local_key)),
                }

    if not cambios:
        return trip

    _validate_dates(trip)

    if commit:
        db.session.commit()
        audit_service.record(
            'trip.updated',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'campos': sorted(cambios)},
        )
    return trip


def change_status(actor, trip, nuevo_estado, motivo=None, commit=True):
    """Move a trip to another state."""
    nuevo_estado = TripStatus.coerce(nuevo_estado)
    if nuevo_estado is None:
        raise ValidationError('El estado indicado no es válido.')

    anterior = trip.estado
    if anterior is nuevo_estado:
        return trip

    trip.estado = nuevo_estado
    if commit:
        db.session.commit()
        audit_service.record(
            'trip.status_changed',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={
                'anterior': str(anterior),
                'nuevo': str(nuevo_estado),
                'motivo': motivo,
            },
        )
    return trip


def delete_trip(actor, trip, commit=True):
    """Soft-delete a trip. The audit trail and documents survive."""
    trip.soft_delete(actor)
    if commit:
        db.session.commit()
        audit_service.record(
            'trip.deleted',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'referencia': trip.referencia},
        )
    return trip


# ======================================================================
# Travellers
# ======================================================================
def add_traveler(actor, trip, user, rol_en_viaje=None, desde_local=None,
                 hasta_local=None, tz=None, observaciones=None, commit=True):
    """Assign a person to a trip (specification flow 5.1 step 2)."""
    from app.models.enums import TravelerRole

    if isinstance(user, (str, bytes)) or hasattr(user, 'hex'):
        import uuid as _uuid

        try:
            user = db.session.get(User, _uuid.UUID(str(user)))
        except (ValueError, TypeError):
            user = None
    if user is None or user.is_deleted:
        raise ResourceNotFound('El usuario indicado no existe.')

    existing = trip.traveler_for(user.id)
    if existing is not None:
        raise ConflictError('Esa persona ya está asignada a este viaje.')

    # Reuse a previously removed assignment instead of leaving a duplicate row,
    # which the unique constraint on (trip_id, user_id) would reject anyway.
    revived = next(
        (t for t in trip.travelers if t.is_deleted and str(t.user_id) == str(user.id)),
        None,
    )
    traveler = revived or TripTraveler(trip_id=trip.id, user_id=user.id)
    if revived is not None:
        traveler.restore()

    traveler.rol_en_viaje = rol_en_viaje or TravelerRole.VIAJERO
    traveler.observaciones = observaciones
    traveler.asignado_por_id = actor.id

    zone = tz or trip.inicio_tz or _default_timezone(actor)
    set_instant(traveler, 'desde', desde_local, zone)
    set_instant(traveler, 'hasta', hasta_local, zone)

    if revived is None:
        db.session.add(traveler)
    trip.touch_itinerary()

    if commit:
        db.session.commit()
        audit_service.record(
            'trip.traveler_added',
            recurso_tipo=AuditResourceType.VIAJERO,
            recurso_id=str(traveler.id),
            actor=actor,
            metadatos={'trip_id': str(trip.id), 'usuario': user.username},
        )
    return traveler


def remove_traveler(actor, trip, user_id, commit=True):
    """Unassign a person from a trip."""
    traveler = trip.traveler_for(user_id)
    if traveler is None:
        raise ResourceNotFound('Esa persona no está asignada a este viaje.')

    traveler.soft_delete(actor)
    trip.touch_itinerary()

    if commit:
        db.session.commit()
        audit_service.record(
            'trip.traveler_removed',
            recurso_tipo=AuditResourceType.VIAJERO,
            recurso_id=str(traveler.id),
            actor=actor,
            metadatos={'trip_id': str(trip.id), 'user_id': str(user_id)},
        )
    return traveler


# ======================================================================
# Destinations
# ======================================================================
def add_destination(actor, trip, ciudad=None, pais_codigo=None, pais_nombre=None,
                    zona_horaria=None, inicio_local=None, fin_local=None,
                    es_escala=False, orden=None, location=None,
                    observaciones=None, commit=True):
    """Append a destination to the trip's itinerary."""
    if orden is None:
        orden = max((d.orden for d in trip.destinations), default=-1) + 1

    zone = zona_horaria or (location.zona_horaria if location else None) \
        or trip.inicio_tz or _default_timezone(actor)

    destination = TripDestination(
        trip_id=trip.id,
        orden=orden,
        ciudad=ciudad or (location.ciudad if location else None),
        pais_codigo=(pais_codigo or (location.pais_codigo if location else None)),
        pais_nombre=pais_nombre,
        location_id=location.id if location else None,
        zona_horaria=zone,
        es_escala=bool(es_escala),
        observaciones=observaciones,
    )
    set_instant(destination, 'inicio', inicio_local, zone)
    set_instant(destination, 'fin', fin_local, zone)

    db.session.add(destination)
    trip.touch_itinerary()

    if commit:
        db.session.commit()
        audit_service.record(
            'trip.destination_added',
            recurso_tipo=AuditResourceType.VIAJE,
            recurso_id=str(trip.id),
            actor=actor,
            metadatos={'ciudad': destination.ciudad, 'pais': destination.pais_codigo},
        )
    return destination


def remove_destination(actor, trip, destination_id, commit=True):
    """Remove a destination and close the gap in the ordering."""
    destination = next(
        (d for d in trip.destinations if str(d.id) == str(destination_id)), None
    )
    if destination is None:
        raise ResourceNotFound('El destino indicado no existe.')

    db.session.delete(destination)
    db.session.flush()
    for index, remaining in enumerate(
        sorted((d for d in trip.destinations if d.id != destination.id),
               key=lambda d: d.orden)
    ):
        remaining.orden = index

    trip.touch_itinerary()
    if commit:
        db.session.commit()
    return True


def itinerary_span(trip):
    """The first and last instants the trip's itinerary covers.

    Returns a ``(primero, ultimo)`` pair of ``(utc, local, tz)`` triples, or
    None when there is no itinerary to read. Both ends come from real rows, so
    each keeps the timezone of its own place: a trip that starts in Barcelona
    and ends in London is not one timezone with two dates.
    """
    instantes = []
    for coleccion, inicio, fin in (
        (trip.segments, 'salida', 'llegada'),
        (trip.accommodations, 'check_in', 'check_out'),
        (trip.vehicle_rentals, 'recogida', 'devolucion'),
        (trip.other_services, 'inicio', 'fin'),
    ):
        for item in coleccion:
            if item.is_deleted:
                continue
            for prefijo in (inicio, fin):
                utc = getattr(item, f'{prefijo}_utc', None)
                if utc is not None:
                    instantes.append((
                        utc,
                        getattr(item, f'{prefijo}_local', None),
                        getattr(item, f'{prefijo}_tz', None),
                    ))

    if not instantes:
        return None

    instantes.sort(key=lambda entry: entry[0])
    return instantes[0], instantes[-1]


def propose_traveler_window(trip):
    """Dates to offer when assigning someone to a trip.

    A manager assigning a traveller to a trip the system already read should
    not retype what the booking documents said. The itinerary is preferred over
    the trip's own dates because it is the evidence: the trip's dates may have
    been typed before any document arrived, or widened on purpose.

    This is a proposal, never a decision -- the fields stay editable and empty
    still means "the whole trip". Returns ``(desde, hasta, origen)`` where each
    date is a naive local datetime and ``origen`` says which source it came
    from, so the interface can tell the manager what it is offering and why.
    """
    extremos = itinerary_span(trip)
    if extremos is not None:
        primero, ultimo = extremos
        if primero[1] is not None and ultimo[1] is not None:
            return primero[1], ultimo[1], 'itinerario'

    if trip.inicio_local and trip.fin_local:
        return trip.inicio_local, trip.fin_local, 'viaje'

    return None, None, None


def sync_destinations_from_itinerary(actor, trip, commit=False):
    """Derive the trip's destinations from where its itinerary actually goes.

    A trip built from booking documents already says where it goes: the flights
    name their arrival airports and the hotels their cities, each resolved
    against the catalogue with a country and a timezone. Making a manager retype
    that is asking them to copy what the system just read -- and until they do,
    the security advisories have no destination to work from.

    Only adds what is missing, never removes: a destination someone entered by
    hand is theirs, and a stopover is not a destination, so an airport a flight
    only passes through is skipped when another leg departs from it.

    Returns the destinations created.
    """
    from app.models.catalog import Country, Location

    existentes = {
        (d.pais_codigo, (d.ciudad or '').lower())
        for d in trip.destinations
    }
    paises_existentes = {d.pais_codigo for d in trip.destinations if d.pais_codigo}

    salidas = {
        s.origen_codigo for s in trip.segments
        if not s.is_deleted and s.origen_codigo
    }

    candidatos = []
    for segmento in trip.segments:
        if segmento.is_deleted or not segmento.destino_codigo:
            continue
        # An airport another leg departs from is a connection, not a stay.
        es_escala = segmento.destino_codigo in salidas
        candidatos.append((
            segmento.destino_codigo, segmento.destino_ciudad,
            segmento.destino_pais, segmento.llegada_tz, es_escala,
        ))

    for alojamiento in trip.accommodations:
        if alojamiento.is_deleted or not alojamiento.ciudad:
            continue
        candidatos.append((
            None, alojamiento.ciudad, alojamiento.pais,
            alojamiento.check_in_tz, False,
        ))

    creados = []
    for codigo, ciudad, pais, zona, es_escala in candidatos:
        if es_escala:
            continue

        location = None
        if codigo:
            location = Location.query.filter_by(codigo=codigo, activo=True).first()
        ciudad = ciudad or (location.ciudad if location else None)
        pais = pais or (location.pais_codigo if location else None)
        if not ciudad and not pais:
            continue

        clave = (pais, (ciudad or '').lower())
        if clave in existentes or (pais and pais in paises_existentes):
            continue

        pais_nombre = None
        if pais:
            fila = Country.query.filter_by(codigo=pais).first()
            pais_nombre = fila.nombre if fila else None

        creados.append(add_destination(
            actor, trip, ciudad=ciudad, pais_codigo=pais,
            pais_nombre=pais_nombre,
            zona_horaria=zona or (location.zona_horaria if location else None),
            location=location, commit=False,
        ))
        existentes.add(clave)
        if pais:
            paises_existentes.add(pais)

    if creados and commit:
        db.session.commit()

    return creados


def sync_dates_from_itinerary(trip, commit=False):
    """Fill the trip's dates from its itinerary when they are still empty.

    A trip created from booking documents has no dates of its own: they are in
    the documents. Once the extracted services are approved the span is known,
    so taking it from there saves retyping what the system already read -- and
    avoids the trip sitting with an "faltan fechas" alert that the manager
    cannot resolve without copying dates by hand.

    Only empty fields are filled. A date a manager typed is never overwritten:
    they may deliberately have set a wider window than the bookings cover.

    Returns the list of fields actually set.
    """
    if trip.inicio_utc is not None and trip.fin_utc is not None:
        return []

    extremos = itinerary_span(trip)
    if extremos is None:
        return []

    primero, ultimo = extremos

    aplicados = []
    if trip.inicio_utc is None and primero[1] is not None:
        set_instant(trip, 'inicio', primero[1], primero[2])
        aplicados.append('inicio')
    if trip.fin_utc is None and ultimo[1] is not None:
        set_instant(trip, 'fin', ultimo[1], ultimo[2])
        aplicados.append('fin')

    if aplicados and commit:
        db.session.commit()

    return aplicados


# ======================================================================
# Listing
# ======================================================================
def list_trips(actor, page=1, per_page=20, estado=None, buscar=None,
               gestor_id=None, desde=None, hasta=None, orden='inicio_desc'):
    """Paginated, filtered trip listing scoped to what the actor may see."""
    from app.services.authorization_service import visible_trips_query

    query = visible_trips_query(actor)

    if estado:
        estados = estado if isinstance(estado, (list, tuple)) else [estado]
        query = query.filter(Trip.estado.in_([str(e) for e in estados]))

    if gestor_id:
        query = query.filter(Trip.gestor_id == gestor_id)

    if buscar:
        needle = f'%{str(buscar).strip().lower()}%'
        query = query.filter(
            db.or_(
                func.lower(Trip.titulo).like(needle),
                func.lower(Trip.referencia).like(needle),
                func.lower(Trip.observaciones).like(needle),
            )
        )

    if desde:
        query = query.filter(Trip.fin_utc >= desde)
    if hasta:
        query = query.filter(Trip.inicio_utc <= hasta)

    orderings = {
        'inicio_desc': Trip.inicio_utc.desc().nullslast(),
        'inicio_asc': Trip.inicio_utc.asc().nullsfirst(),
        'referencia_desc': Trip.referencia.desc(),
        'creado_desc': Trip.created_at.desc(),
    }
    query = query.order_by(orderings.get(orden, orderings['inicio_desc']))

    return query.paginate(page=page, per_page=per_page, error_out=False)


def get_or_404(trip_id):
    """Fetch a trip by id or raise :class:`ResourceNotFound`."""
    import uuid as _uuid

    try:
        trip = db.session.get(Trip, _uuid.UUID(str(trip_id)))
    except (ValueError, TypeError):
        trip = None
    if trip is None or trip.is_deleted:
        raise ResourceNotFound('El viaje solicitado no existe.')
    return trip


# ======================================================================
# Helpers
# ======================================================================
def _validate_dates(trip):
    """A trip cannot end before it starts."""
    if trip.inicio_utc and trip.fin_utc and trip.fin_utc < trip.inicio_utc:
        raise ValidationError(
            'La fecha de fin del viaje no puede ser anterior a la de inicio.'
        )


def _default_timezone(actor):
    """The timezone to assume when none was given."""
    from flask import current_app

    return (
        getattr(actor, 'zona_horaria', None)
        or current_app.config.get('DEFAULT_TIMEZONE', 'Europe/Madrid')
    )


def _readable(value):
    """Render a value for the audit metadata."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)[:200]
