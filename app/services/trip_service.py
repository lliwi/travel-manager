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
                referencia=None, proyecto=None, coste_estimado=None,
                moneda=None, commit=True):
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
        proyecto=(str(proyecto).strip()[:120] or None) if proyecto else None,
        coste_estimado=coste_estimado,
        moneda=(str(moneda).strip().upper()[:3] or None) if moneda else None,
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
    # «estado» no está aquí a propósito: lo escribe «change_status», que es
    # quien conoce las transiciones. Escribirlo también desde aquí dejaría al
    # formulario de edición saltándose todas las reglas.
    simple_fields = (
        'titulo', 'finalidad', 'finalidad_detalle', 'observaciones',
        'gestor_id', 'proyecto', 'coste_estimado', 'moneda',
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


#: Which state a trip may move to from each one. Everything absent is refused.
#:
#: Before this map every transition was accepted -- «finalizado» back to
#: «borrador», «cancelado» to «confirmado» -- because the edit form offered the
#: six states in a free select and nothing looked at the one it came from. A
#: state that can be set to anything describes nothing, and this application
#: already decides real things from it: the alert engine and the advisories
#: skip closed trips, so marking one finished turns off its watch.
TRANSICIONES = {
    TripStatus.BORRADOR: (
        TripStatus.EN_PREPARACION, TripStatus.CONFIRMADO, TripStatus.CANCELADO,
    ),
    TripStatus.EN_PREPARACION: (
        TripStatus.BORRADOR, TripStatus.CONFIRMADO,
        # Por fecha, desde la tarea programada: un viaje cuya ventana pasó está
        # terminado aunque nadie llegara a confirmarlo.
        TripStatus.EN_CURSO, TripStatus.FINALIZADO,
        TripStatus.CANCELADO,
    ),
    TripStatus.CONFIRMADO: (
        # Hacia atrás solo aquí: se cayó una reserva y vuelve a prepararse.
        TripStatus.EN_PREPARACION,
        TripStatus.EN_CURSO, TripStatus.FINALIZADO, TripStatus.CANCELADO,
    ),
    TripStatus.EN_CURSO: (TripStatus.FINALIZADO, TripStatus.CANCELADO),
    #: Terminal. Un viaje que ya ocurrió no vuelve a ocurrir.
    TripStatus.FINALIZADO: (),
    #: Cancelar es una decisión, no una etapa, y se puede deshacer: vuelve a
    #: prepararse, nunca directamente a confirmado, porque lo que hubiera
    #: reservado hay que volver a mirarlo.
    TripStatus.CANCELADO: (TripStatus.EN_PREPARACION,),
}

#: Reactivar exige decir por qué: es lo que distingue un cambio de opinión de
#: un clic equivocado cuando alguien lo lea dentro de seis meses.
ESTADOS_QUE_EXIGEN_MOTIVO = (TripStatus.CANCELADO,)


def requisitos_pendientes(trip, destino):
    """What a trip still lacks before it may reach ``destino``.

    Only «confirmado» asks for anything, and it asks for the three things the
    word claims: when it is, who goes, and how they get there. Without them
    «confirmado» would not mean anything and no report could lean on it.

    Returns a list of sentences to show, empty when there is nothing missing.
    """
    if TripStatus.coerce(destino) is not TripStatus.CONFIRMADO:
        return []

    faltan = []
    if not trip.inicio_utc or not trip.fin_utc:
        faltan.append('indicar las fechas de inicio y fin')
    if not trip.active_travelers:
        faltan.append('asignar al menos una persona viajera')
    if not _tiene_itinerario(trip):
        faltan.append('registrar algún trayecto, alojamiento o servicio')
    return faltan


def _tiene_itinerario(trip):
    """Whether anything at all has been planned for this trip."""
    return any(
        any(not item.is_deleted for item in coleccion)
        for coleccion in (trip.segments, trip.accommodations,
                          trip.vehicle_rentals, trip.other_services)
    )


def estados_posibles(trip):
    """The states this trip may be moved to right now, for a select field."""
    return [
        destino for destino in TRANSICIONES.get(trip.estado, ())
        # Los que llegan solos por fecha no se ofrecen a mano: ponerlos antes
        # de tiempo diría que un viaje ha empezado cuando no ha empezado.
        if destino is not TripStatus.EN_CURSO
    ]


def change_status(actor, trip, nuevo_estado, motivo=None, commit=True,
                  automatico=False):
    """Move a trip to another state, if it may go there.

    The single door: ``update_trip`` routes here rather than writing the column,
    because a rule that the edit form can walk around is not a rule.

    ``automatico`` is for the changes nobody decides -- the dates passing, the
    first document arriving -- which are not held to the manual requirements:
    a trip whose window has passed is over whether or not anybody filled in its
    itinerary.
    """
    nuevo_estado = TripStatus.coerce(nuevo_estado)
    if nuevo_estado is None:
        raise ValidationError('El estado indicado no es válido.')

    anterior = trip.estado
    if anterior is nuevo_estado:
        return trip

    if nuevo_estado not in TRANSICIONES.get(anterior, ()):
        raise ConflictError(
            f'Un viaje «{anterior.label}» no puede pasar a '
            f'«{nuevo_estado.label}».'
        )

    if not automatico:
        faltan = requisitos_pendientes(trip, nuevo_estado)
        if faltan:
            raise ValidationError(
                f'Para confirmar el viaje falta {", y ".join(faltan)}.'
            )

        if anterior in ESTADOS_QUE_EXIGEN_MOTIVO and not (motivo or '').strip():
            raise ValidationError(
                'Indique por qué se reactiva un viaje cancelado.'
            )

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


def marcar_actividad(trip, actor=None, motivo=None, commit=True):
    """Leave «borrador» behind the moment somebody starts working on the trip.

    Attaching a booking, assigning a traveller or writing a leg are all the
    same statement: this is no longer a sketch. Asking somebody to *also*
    remember to move the state is how a list fills with drafts that are
    actually under way, and a state nobody trusts is a state nobody reads.

    Does nothing outside «borrador» -- it cannot pull a cancelled trip back to
    life, and it cannot undo a confirmation.
    """
    if trip is None or trip.estado is not TripStatus.BORRADOR:
        return trip

    return change_status(
        actor, trip, TripStatus.EN_PREPARACION, automatico=True,
        motivo=motivo or 'Se ha empezado a trabajar en el viaje.',
        commit=commit,
    )


#: States a trip advances out of on its own. A draft is deliberately not one:
#: its dates may be a guess, and announcing a trip nobody confirmed as «en
#: curso» would put it in front of travellers on the strength of a placeholder.
ESTADOS_QUE_AVANZAN = (TripStatus.EN_PREPARACION, TripStatus.CONFIRMADO)


def advance_states(ahora=None, commit=True):
    """Move trips through «en curso» and «finalizado» as their dates pass.

    Some state changes are nobody's decision: a trip whose first flight left
    this morning is under way whether or not a manager remembered to say so,
    and the alert engine skips closed trips, so one left as «confirmado» for
    months keeps being recalculated for a journey that already happened.

    Cancelled trips are never touched: that state is a decision, not a stage.

    Returns ``{'en_curso': n, 'finalizado': n}``.
    """
    ahora = ahora or utcnow()
    aplicados = {'en_curso': 0, 'finalizado': 0}

    # Finish first, so a trip whose whole window has passed lands on its final
    # state in one pass instead of waiting for the next tick.
    terminados = Trip.query.filter(
        Trip.is_deleted.is_(False),
        Trip.estado.in_([str(e) for e in ESTADOS_QUE_AVANZAN] + [str(TripStatus.EN_CURSO)]),
        Trip.fin_utc.isnot(None),
        Trip.fin_utc < ahora,
    ).all()
    for trip in terminados:
        # Committed one at a time so each carries its audit entry: a state
        # that changed itself is exactly the kind a reader will later want
        # explained.
        change_status(
            None, trip, TripStatus.FINALIZADO, automatico=True,
            motivo='La fecha de fin del viaje ya ha pasado.', commit=commit,
        )
        aplicados['finalizado'] += 1

    empezados = Trip.query.filter(
        Trip.is_deleted.is_(False),
        Trip.estado.in_([str(e) for e in ESTADOS_QUE_AVANZAN]),
        Trip.inicio_utc.isnot(None),
        Trip.inicio_utc <= ahora,
    ).all()
    for trip in empezados:
        change_status(
            None, trip, TripStatus.EN_CURSO, automatico=True,
            motivo='La fecha de inicio del viaje ya ha llegado.', commit=commit,
        )
        aplicados['en_curso'] += 1

    return aplicados


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
    marcar_actividad(trip, actor, commit=False)

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


def _punto_de_partida(tramos):
    """Where the journey starts, by the earliest departure."""
    con_hora = [s for s in tramos if s.salida_utc is not None and s.origen_codigo]
    if not con_hora:
        return None
    return min(con_hora, key=lambda s: s.salida_utc).origen_codigo


def _es_escala(segmento, tramos):
    """Whether a leg's arrival is somewhere passed through, not stayed in.

    Being the origin of a later leg is not enough: on a return booking the
    destination is always also where the journey home departs from, so that
    test threw away the one place the trip is actually about -- and with it the
    country the security advisories are built from.

    What separates the two is time. If the next departure from there is soon
    enough to be a connection, nobody stayed; otherwise they did.
    """
    from app.services.itinerary_service import es_conexion
    from app.utils.timeutil import minutes_between

    if segmento.llegada_utc is None:
        return False

    siguientes = [
        s.salida_utc for s in tramos
        if s is not segmento
        and s.origen_codigo == segmento.destino_codigo
        and s.salida_utc is not None
        and s.salida_utc >= segmento.llegada_utc
    ]
    if not siguientes:
        return False

    return es_conexion(minutes_between(segmento.llegada_utc, min(siguientes)))


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

    tramos = [s for s in trip.segments if not s.is_deleted]
    origen = _punto_de_partida(tramos)

    candidatos = []
    for segmento in tramos:
        if not segmento.destino_codigo:
            continue
        # Arriving back where the journey started is coming home, not going
        # somewhere: a return booking would otherwise make the traveller's own
        # city a destination with advisories of its own.
        if origen and segmento.destino_codigo == origen:
            continue
        candidatos.append((
            segmento.destino_codigo, segmento.destino_ciudad,
            segmento.destino_pais, segmento.llegada_tz,
            _es_escala(segmento, tramos),
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

        # Through the catalogue, so a hotel that says «London» and a flight
        # whose airport the catalogue files under «Londres» do not become two
        # destinations -- one of them without a country to look anything up by.
        if ciudad:
            from app.services.normalization_service import resolver_lugar

            zona_catalogo, pais_catalogo, ciudad_catalogo = resolver_lugar(
                [ciudad], pais,
            )
            ciudad = ciudad_catalogo or ciudad
            pais = pais or pais_catalogo
            zona = zona or zona_catalogo

        if not ciudad and not pais:
            continue

        clave = (pais, (ciudad or '').lower())
        ciudades_existentes = {c for _, c in existentes if c}
        if clave in existentes or (pais and pais in paises_existentes):
            continue
        # A candidate whose country could not be resolved still duplicates one
        # that names the same city; keeping both would ask for advisories twice
        # and once without a country to look up.
        if not pais and (ciudad or '').lower() in ciudades_existentes:
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
    from app.services import settings_service

    return (
        getattr(actor, 'zona_horaria', None)
        or settings_service.zona_horaria_por_defecto()
    )


def _readable(value):
    """Render a value for the audit metadata."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)[:200]
