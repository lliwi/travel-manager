"""Where the travellers are, by city, on one day.

The question this answers is the duty-of-care one -- «if something happens in
Estambul tonight, do I have anybody there» -- so it is built to be *complete*
rather than tidy. A traveller the itinerary cannot place is reported as
unplaced, never dropped: a map that quietly omits three people is worse than no
map, because it answers the question with false confidence.

**Scoping.** Like every other report, it starts from ``visible_trips_query``.
On top of that, somebody who is not a manager or an administrator sees only
their own position: an aggregate is a leak like any other, and «tres personas
en Dubái» told to a colleague says where three named people sleep tonight.

**Why the coordinates live here and not in ``locations``.** That table's
``latitud``/``longitud`` describe an *airport or station*; Barajas is twelve
kilometres from the middle of Madrid. What the map needs is the city, so the
city is what is written down. Filling the airport's columns with the city's
centre would be a small lie that some later distance calculation would believe.
"""
import logging
import unicodedata
from datetime import date, datetime, time, timedelta

from app.extensions import db
from app.models.enums import RoleCode, TripStatus
from app.models.itinerary import Accommodation, TravelSegment
from app.models.trip import Trip, TripDestination, TripTraveler
from app.models.user import User
from app.services.authorization_service import visible_trips_query
from app.utils.timeutil import UTC, as_aware_utc, utcnow

logger = logging.getLogger(__name__)

#: City centres, one entry per city in the seeded catalogue. Two decimals is
#: about a kilometre, which on a world map is a fifth of a pixel.
COORDENADAS = {
    ('madrid', 'ES'): (40.42, -3.70),
    ('barcelona', 'ES'): (41.39, 2.17),
    ('malaga', 'ES'): (36.72, -4.42),
    ('valencia', 'ES'): (39.47, -0.38),
    ('sevilla', 'ES'): (37.39, -5.98),
    ('bilbao', 'ES'): (43.26, -2.93),
    ('palma', 'ES'): (39.57, 2.65),
    ('alicante', 'ES'): (38.35, -0.48),
    ('las palmas', 'ES'): (28.12, -15.43),
    ('tenerife', 'ES'): (28.47, -16.25),
    ('santiago de compostela', 'ES'): (42.88, -8.54),
    ('zaragoza', 'ES'): (41.65, -0.89),
    ('lisboa', 'PT'): (38.72, -9.14),
    ('oporto', 'PT'): (41.15, -8.61),
    ('paris', 'FR'): (48.86, 2.35),
    ('londres', 'GB'): (51.51, -0.13),
    ('amsterdam', 'NL'): (52.37, 4.90),
    ('bruselas', 'BE'): (50.85, 4.35),
    ('berlin', 'DE'): (52.52, 13.40),
    ('francfort', 'DE'): (50.11, 8.68),
    ('munich', 'DE'): (48.14, 11.58),
    ('dusseldorf', 'DE'): (51.23, 6.78),
    ('roma', 'IT'): (41.90, 12.50),
    ('milan', 'IT'): (45.46, 9.19),
    ('viena', 'AT'): (48.21, 16.37),
    ('zurich', 'CH'): (47.38, 8.54),
    ('ginebra', 'CH'): (46.20, 6.14),
    ('copenhague', 'DK'): (55.68, 12.57),
    ('estocolmo', 'SE'): (59.33, 18.07),
    ('oslo', 'NO'): (59.91, 10.75),
    ('helsinki', 'FI'): (60.17, 24.94),
    ('dublin', 'IE'): (53.35, -6.26),
    ('praga', 'CZ'): (50.08, 14.44),
    ('varsovia', 'PL'): (52.23, 21.01),
    ('budapest', 'HU'): (47.50, 19.04),
    ('atenas', 'GR'): (37.98, 23.73),
    ('estambul', 'TR'): (41.01, 28.98),
    ('casablanca', 'MA'): (33.57, -7.59),
    ('johannesburgo', 'ZA'): (-26.20, 28.05),
    ('dubai', 'AE'): (25.20, 55.27),
    ('tel aviv', 'IL'): (32.08, 34.78),
    ('bombay', 'IN'): (19.08, 72.88),
    ('nueva delhi', 'IN'): (28.61, 77.21),
    ('pekin', 'CN'): (39.90, 116.41),
    ('shanghai', 'CN'): (31.23, 121.47),
    ('seul', 'KR'): (37.57, 126.98),
    ('tokio', 'JP'): (35.68, 139.69),
    ('singapur', 'SG'): (1.35, 103.82),
    ('sidney', 'AU'): (-33.87, 151.21),
    ('auckland', 'NZ'): (-36.85, 174.76),
    ('nueva york', 'US'): (40.71, -74.01),
    ('chicago', 'US'): (41.88, -87.63),
    ('los angeles', 'US'): (34.05, -118.24),
    ('san francisco', 'US'): (37.77, -122.42),
    ('miami', 'US'): (25.76, -80.19),
    ('washington', 'US'): (38.91, -77.04),
    ('toronto', 'CA'): (43.65, -79.38),
    ('ciudad de mexico', 'MX'): (19.43, -99.13),
    ('bogota', 'CO'): (4.71, -74.07),
    ('lima', 'PE'): (-12.05, -77.04),
    ('santiago', 'CL'): (-33.45, -70.67),
    ('buenos aires', 'AR'): (-34.60, -58.38),
    ('sao paulo', 'BR'): (-23.55, -46.63),
}

#: The same crop the background map is drawn with. Both have to agree or every
#: marker lands somewhere else, so the numbers are stated once and imported.
LAT_NORTE = 84.0
LAT_SUR = -58.0

#: Trips in these states have no travellers anywhere.
ESTADOS_FUERA = (TripStatus.BORRADOR, TripStatus.CANCELADO)


def _sin_acentos(texto):
    """Lowercase and strip accents, so «Zúrich» and «zurich» are one key."""
    plano = unicodedata.normalize('NFKD', str(texto or ''))
    return ''.join(c for c in plano if not unicodedata.combining(c)).strip().lower()


def coordenadas_de(ciudad, pais):
    """The city's position, or None when it is not one we can place.

    None rather than a guess: a marker in the wrong place is read with the same
    confidence as a right one.

    Falls back to the name alone when the country is missing, because that is
    how the data actually arrives -- an extracted leg carries «Londres» and no
    country perfectly often. It only does so when the name is unambiguous
    across the table; ``tests`` pin that it is, so the day a second «Córdoba»
    is added the fallback refuses instead of pointing at the wrong country.
    """
    if not ciudad:
        return None

    clave = _sin_acentos(ciudad)
    punto = COORDENADAS.get((clave, (pais or '').upper()))
    if punto is not None:
        return punto
    if pais:
        return None

    candidatos = {p for (nombre, _), p in COORDENADAS.items() if nombre == clave}
    return candidatos.pop() if len(candidatos) == 1 else None


def ciudad_del_catalogo(codigo=None, location_id=None, nombres=(), pais=None):
    """The catalogue's own spelling of a place, from whatever the row carries.

    This is the whole answer to «London is not Londres». A booking gives a
    code, a free-text city in the language of the document, or both, and the
    catalogue is what knows they are the same place -- so the catalogue is
    asked rather than the text compared. Order is by how much each source can
    be trusted: the resolved foreign key, then the code on the ticket, then the
    name, which is the only one a person can have typed.

    Returns ``(ciudad, pais)`` as the catalogue spells them, or the values it
    was given when the catalogue knows nothing about them -- never None for
    both, so the caller can still report the traveller as unplaceable *with the
    name that was on the document*.
    """
    from app.models.catalog import Location
    from app.services.normalization_service import resolver_lugar

    fila = None
    if location_id is not None:
        fila = Location.query.get(location_id)
    if fila is None and codigo:
        fila = Location.query.filter_by(
            codigo=str(codigo).strip().upper(), activo=True,
        ).first()
    if fila is not None and fila.ciudad:
        return fila.ciudad, fila.pais_codigo or pais

    utiles = [n for n in nombres if n]
    if utiles:
        _, pais_resuelto, ciudad_resuelta = resolver_lugar(utiles, pais)
        if ciudad_resuelta:
            return ciudad_resuelta, pais_resuelto or pais

    return (utiles[0] if utiles else None), pais


def proyectar(latitud, longitud):
    """Equirectangular, in percentages of the drawing box.

    Percentages rather than pixels because the panel is responsive: the marker
    has to stay on its city when the column is narrower.
    """
    x = (float(longitud) + 180.0) / 360.0 * 100.0
    y = (LAT_NORTE - float(latitud)) / (LAT_NORTE - LAT_SUR) * 100.0
    return round(x, 3), round(y, 3)


def _limites_del_dia(dia):
    """The UTC day, as the half-open interval the queries compare against."""
    inicio = datetime.combine(dia, time.min, tzinfo=UTC)
    return inicio, inicio + timedelta(days=1)


def _solo_los_suyos(actor):
    """Whether this actor may see other people's positions."""
    return not (actor.has_role(RoleCode.ADMINISTRADOR)
                or actor.has_role(RoleCode.GESTOR))


def _consulta_de_viajeros(actor, inicio, fin):
    """Travellers on the road at some point in ``[inicio, fin)``.

    The single definition of «está de viaje», used both by the day the map
    draws and by the month the calendar marks. Two queries asking the same
    question in two places is how a calendar ends up highlighting a day the map
    then reports as empty.

    A traveller with no dates of their own follows the trip's, which is what
    «asignado al viaje» means when nobody narrowed it.
    """
    visibles = db.select(visible_trips_query(actor).with_entities(Trip.id).subquery().c.id)

    consulta = (
        db.session.query(TripTraveler, Trip, User)
        .join(Trip, Trip.id == TripTraveler.trip_id)
        .join(User, User.id == TripTraveler.user_id)
        .filter(
            TripTraveler.trip_id.in_(visibles),
            TripTraveler.is_deleted.is_(False),
            Trip.estado.notin_([str(e) for e in ESTADOS_FUERA]),
            db.or_(TripTraveler.desde_utc.is_(None), TripTraveler.desde_utc < fin),
            db.or_(TripTraveler.hasta_utc.is_(None), TripTraveler.hasta_utc > inicio),
            db.or_(Trip.inicio_utc.is_(None), Trip.inicio_utc < fin),
            db.or_(Trip.fin_utc.is_(None), Trip.fin_utc > inicio),
        )
    )

    if _solo_los_suyos(actor):
        consulta = consulta.filter(TripTraveler.user_id == actor.id)

    return consulta


def _viajeros_en_curso(actor, inicio, fin):
    """The same question, answered for one day, keyed by traveller."""
    return {
        viajero.id: {'viajero': viajero, 'viaje': viaje, 'usuario': usuario}
        for viajero, viaje, usuario in _consulta_de_viajeros(actor, inicio, fin).all()
    }


def dias_con_viajeros(actor, mes):
    """How many people are away on each day of ``mes``.

    One query for the whole month rather than thirty days of the map's own
    query: the calendar exists to be glanced at, and a panel that costs thirty
    round trips is a panel somebody turns off.

    Each traveller's effective window is their own dates narrowed by the
    trip's, which is what ``_consulta_de_viajeros`` already filters by -- the
    same rule, applied here to fill in the days between.
    """
    primero = mes.replace(day=1)
    siguiente = (primero + timedelta(days=32)).replace(day=1)
    inicio, fin = _limites_del_dia(primero)[0], _limites_del_dia(siguiente)[0]

    cuenta = {}
    for viajero, viaje, _usuario in _consulta_de_viajeros(actor, inicio, fin).all():
        desde = _mayor(viajero.desde_utc, viaje.inicio_utc) or inicio
        hasta = _menor(viajero.hasta_utc, viaje.fin_utc) or fin

        dia = max(as_aware_utc(desde), inicio).date()
        ultimo = min(as_aware_utc(hasta), fin).date()
        # A stay ending at 00:00 does not put anybody there that day.
        if as_aware_utc(hasta).time() == time.min and ultimo > dia:
            ultimo -= timedelta(days=1)

        while dia <= ultimo and dia < siguiente:
            if dia >= primero:
                cuenta[dia] = cuenta.get(dia, 0) + 1
            dia += timedelta(days=1)

    return cuenta


#: Monday first, which is how a week is written here.
DIAS_DE_LA_SEMANA = ('L', 'M', 'X', 'J', 'V', 'S', 'D')

MESES = (
    'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio',
    'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre',
)


def calendario_del_mes(actor, dia, abierto=False):
    """The month around ``dia``, with the days somebody is away marked.

    Whole weeks, so the grid is rectangular and the columns line up under their
    weekday; the days spilling in from the neighbouring months are drawn faint
    rather than left blank, because a hole in the corner of a calendar reads as
    a rendering fault.
    """
    import calendar as _cal

    primero = dia.replace(day=1)
    siguiente = (primero + timedelta(days=32)).replace(day=1)
    cuenta = dias_con_viajeros(actor, primero)
    hoy = utcnow().date()

    semanas = []
    for semana in _cal.Calendar(firstweekday=0).monthdatescalendar(
        primero.year, primero.month,
    ):
        semanas.append([
            {
                'fecha': fecha,
                'total': cuenta.get(fecha, 0),
                'del_mes': primero <= fecha < siguiente,
                'es_hoy': fecha == hoy,
                'es_el_elegido': fecha == dia,
            }
            for fecha in semana
        ])

    return {
        'mes': primero,
        # Mayúscula solo en la primera letra: «text-capitalize» la pone
        # en todas las palabras y escribía «Junio De 2026».
        'titulo': f'{MESES[primero.month - 1].capitalize()} de {primero.year}',
        'anterior': (primero - timedelta(days=1)).replace(day=1),
        'siguiente': siguiente,
        'semanas': semanas,
        'dias': DIAS_DE_LA_SEMANA,
        'maximo': max(cuenta.values(), default=0),
        'total_del_mes': len({f for f, n in cuenta.items() if n}),
        # Cambiar de mes recarga la página. Sin esto el panel se cerraría en
        # cada clic y habría que volver a abrirlo para seguir buscando.
        'abierto': bool(abierto),
    }


def _mayor(*valores):
    """The latest of the instants given, ignoring the ones that are absent."""
    presentes = [as_aware_utc(v) for v in valores if v is not None]
    return max(presentes) if presentes else None


def _menor(*valores):
    presentes = [as_aware_utc(v) for v in valores if v is not None]
    return min(presentes) if presentes else None


def _por_alojamiento(trip_ids, inicio, fin):
    """City per traveller from the lodging that covers that night."""
    filas = (
        Accommodation.query.filter(
            Accommodation.trip_id.in_(trip_ids),
            Accommodation.is_deleted.is_(False),
            Accommodation.check_in_utc < fin,
            Accommodation.check_out_utc > inicio,
        )
        .order_by(Accommodation.check_in_utc.asc())
        .all()
    )
    salida = []
    for f in filas:
        ciudad, pais = ciudad_del_catalogo(
            location_id=f.location_id, nombres=(f.ciudad,), pais=f.pais,
        )
        salida.append((f.trip_id, f.trip_traveler_id, ciudad, pais, 'alojamiento'))
    return salida


def _por_tramo(trip_ids, fin):
    """City per traveller from the last leg that had landed by then."""
    filas = (
        TravelSegment.query.filter(
            TravelSegment.trip_id.in_(trip_ids),
            TravelSegment.is_deleted.is_(False),
            TravelSegment.llegada_utc.isnot(None),
            TravelSegment.llegada_utc < fin,
        )
        .order_by(TravelSegment.llegada_utc.asc())
        .all()
    )
    salida = []
    for f in filas:
        # The code is the reliable half of an extracted leg: a booking says
        # LGW, and whether it also says «London», «Londres» or nothing at all
        # depends on the language of the PDF it came from.
        ciudad, pais = ciudad_del_catalogo(
            codigo=f.destino_codigo,
            location_id=f.destino_location_id,
            nombres=(f.destino_ciudad, f.destino_nombre),
            pais=f.destino_pais,
        )
        salida.append((f.trip_id, f.trip_traveler_id, ciudad, pais, 'tramo'))
    return salida


def _por_salida_del_dia(trip_ids, inicio, fin):
    """City per traveller from where a leg leaving that day is going.

    From the day somebody sets off, the question «who do I have in Londres» is
    about them: by tonight that is where they are, or where they are headed.
    Answering with the city they left -- or with nothing, when that city is not
    one the map can place -- hid them on exactly the day they started to
    matter. A leg still airborne at midnight is flagged as such, so the map
    never claims they have arrived.
    """
    filas = (
        TravelSegment.query.filter(
            TravelSegment.trip_id.in_(trip_ids),
            TravelSegment.is_deleted.is_(False),
            TravelSegment.salida_utc >= inicio,
            TravelSegment.salida_utc < fin,
        )
        # Ascending, so the pass that overwrites leaves the day's last leg:
        # after a connection, the city they end the day in.
        .order_by(TravelSegment.salida_utc.asc())
        .all()
    )
    salida = []
    for f in filas:
        ciudad, pais = ciudad_del_catalogo(
            codigo=f.destino_codigo,
            location_id=f.destino_location_id,
            nombres=(f.destino_ciudad, f.destino_nombre),
            pais=f.destino_pais,
        )
        llega_ese_dia = f.llegada_utc is not None and as_aware_utc(f.llegada_utc) < fin
        if llega_ese_dia:
            salida.append((f.trip_id, f.trip_traveler_id, ciudad, pais, 'tramo'))
        else:
            salida.append((f.trip_id, f.trip_traveler_id, ciudad, pais, 'en_transito',
                           f.llegada_utc))
    return salida


def _por_proximo_tramo(trip_ids, inicio):
    """City per traveller from where their next leg departs.

    The weakest of the four and the one that rescues the commonest itinerary
    there is: a trip with only the flight home recorded. Somebody flying out of
    London on the 2nd was in London on the 30th, which is what anybody reading
    the itinerary would say -- and saying nothing instead was reporting a
    traveller as unlocatable while the answer sat in the next row.

    Only inside the trip's own dates, which the caller has already required:
    outside them «where they take off from next week» says nothing about today.
    """
    filas = (
        TravelSegment.query.filter(
            TravelSegment.trip_id.in_(trip_ids),
            TravelSegment.is_deleted.is_(False),
            TravelSegment.salida_utc.isnot(None),
            TravelSegment.salida_utc >= inicio,
        )
        # Descending, so the pass that overwrites leaves the *earliest* next
        # leg -- the nearest one in time, not the last of the trip.
        .order_by(TravelSegment.salida_utc.desc())
        .all()
    )
    salida = []
    for f in filas:
        ciudad, pais = ciudad_del_catalogo(
            codigo=f.origen_codigo,
            location_id=f.origen_location_id,
            nombres=(f.origen_ciudad, f.origen_nombre),
            pais=f.origen_pais,
        )
        salida.append((f.trip_id, f.trip_traveler_id, ciudad, pais, 'proximo_tramo'))
    return salida


def _por_destino(trip_ids, inicio, fin):
    """City per trip from the declared destination whose window covers the day."""
    filas = (
        TripDestination.query.filter(
            TripDestination.trip_id.in_(trip_ids),
            TripDestination.es_escala.is_(False),
            db.or_(TripDestination.inicio_utc.is_(None), TripDestination.inicio_utc < fin),
            db.or_(TripDestination.fin_utc.is_(None), TripDestination.fin_utc > inicio),
        )
        .order_by(TripDestination.orden.asc())
        .all()
    )
    salida = []
    for f in filas:
        ciudad, pais = ciudad_del_catalogo(
            location_id=f.location_id, nombres=(f.ciudad,), pais=f.pais_codigo,
        )
        salida.append((f.trip_id, None, ciudad, pais, 'destino'))
    return salida


def _en_transito(trip_ids, inicio, fin):
    """Legs that were under way that day: departed before it ended, land after
    it began.

    Consulted only for travellers nothing else placed. Somebody mid-journey is
    in no city, and putting them in the one they are flying to would say they
    had arrived -- but «sin nada en el itinerario que diga dónde estaban» reads
    as missing data when the itinerary in fact says exactly where they are.

    It is also what a badly extracted leg looks like: this is how a flight
    whose departure and arrival came from two different bookings shows up, as
    five days in the air.
    """
    filas = (
        TravelSegment.query.filter(
            TravelSegment.trip_id.in_(trip_ids),
            TravelSegment.is_deleted.is_(False),
            TravelSegment.salida_utc.isnot(None),
            TravelSegment.salida_utc < fin,
            TravelSegment.llegada_utc > inicio,
        )
        .order_by(TravelSegment.salida_utc.asc())
        .all()
    )
    salida = []
    for f in filas:
        ciudad, pais = ciudad_del_catalogo(
            codigo=f.destino_codigo,
            location_id=f.destino_location_id,
            nombres=(f.destino_ciudad, f.destino_nombre),
            pais=f.destino_pais,
        )
        salida.append((f.trip_id, f.trip_traveler_id, ciudad, pais, 'en_transito',
                       f.llegada_utc))
    return salida


def _asignar(candidatos, viajeros_por_viaje):
    """Spread each row over the travellers it speaks for.

    A row with no ``trip_traveler_id`` applies to everybody on the trip, which
    is what the nullable column means throughout the itinerary.
    """
    asignado = {}
    for candidato in candidatos:
        trip_id, traveler_id, ciudad, pais, origen = candidato[:5]
        llegada = candidato[5] if len(candidato) > 5 else None
        if not ciudad:
            continue
        destinatarios = (
            [traveler_id] if traveler_id is not None
            else list(viajeros_por_viaje.get(trip_id, ()))
        )
        for destino in destinatarios:
            asignado[destino] = (ciudad, pais, origen, llegada)
    return asignado


def donde_estan(actor, dia=None):
    """Travellers per city on ``dia``, plus everyone the itinerary cannot place.

    The rule, in order, and said on the screen so a number nobody can explain
    never appears: the lodging covering that night; failing that, where a leg
    leaving that day is going; failing that, the destination of the last leg
    that had landed; failing that, the trip's declared destination; and last,
    where their next leg departs from.
    """
    dia = dia or utcnow().date()
    inicio, fin = _limites_del_dia(dia)

    viajeros = _viajeros_en_curso(actor, inicio, fin)
    if not viajeros:
        return {'dia': dia, 'ciudades': [], 'sin_situar': [], 'sin_ubicacion': [],
                'en_transito': [], 'total': 0, 'maximo': 0}

    viajeros_por_viaje = {}
    for traveler_id, datos in viajeros.items():
        viajeros_por_viaje.setdefault(datos['viaje'].id, []).append(traveler_id)

    trip_ids = list(viajeros_por_viaje)

    # Weakest first: each pass overwrites the one before, so the lodging wins.
    # The next leg is the weakest of the four because it is the only one that
    # is not about this day at all -- «tomorrow you take off from Madrid» says
    # less about today than «the itinerary puts you in Berlin today» does. It
    # is there for the trip that has nothing else, which is the common case.
    ubicacion = {}
    for candidatos in (_por_proximo_tramo(trip_ids, inicio),
                       _por_destino(trip_ids, inicio, fin),
                       _por_tramo(trip_ids, fin),
                       _por_salida_del_dia(trip_ids, inicio, fin),
                       _por_alojamiento(trip_ids, inicio, fin)):
        ubicacion.update(_asignar(candidatos, viajeros_por_viaje))

    # Only for whoever is left: somebody who landed today is placed by the
    # arrival, not reported as flying.
    transito = _asignar(_en_transito(trip_ids, inicio, fin), viajeros_por_viaje)

    ciudades, sin_situar, sin_ubicacion, en_transito = {}, [], [], []

    for traveler_id, datos in viajeros.items():
        persona = {
            'nombre': datos['usuario'].nombre_completo,
            'viaje_id': str(datos['viaje'].id),
            'viaje': datos['viaje'].referencia,
        }
        situacion = ubicacion.get(traveler_id) or transito.get(traveler_id)
        if situacion is None:
            sin_ubicacion.append(persona)
            continue

        ciudad, pais, origen, llegada = situacion
        punto = coordenadas_de(ciudad, pais)
        persona['segun'] = origen
        if origen == 'en_transito':
            # Cuenta en su ciudad de destino, que es lo que se pregunta cuando
            # no hay alojamiento, pero sin dejar de decir que todavía va de
            # camino: si no, el mapa afirmaría que ha llegado.
            persona['llega'] = llegada
            en_transito.append(persona)
        if punto is None:
            sin_situar.append({**persona, 'ciudad': ciudad, 'pais': pais})
            continue

        clave = (_sin_acentos(ciudad), (pais or '').upper())
        entrada = ciudades.setdefault(clave, {
            'ciudad': ciudad, 'pais': pais,
            'latitud': punto[0], 'longitud': punto[1],
            'personas': [],
        })
        entrada['personas'].append(persona)

    filas = sorted(ciudades.values(),
                   key=lambda c: (-len(c['personas']), _sin_acentos(c['ciudad'])))
    for fila in filas:
        fila['total'] = len(fila['personas'])
        fila['x'], fila['y'] = proyectar(fila['latitud'], fila['longitud'])

    return {
        'dia': dia,
        'ciudades': filas,
        'sin_situar': sin_situar,
        'sin_ubicacion': sin_ubicacion,
        'en_transito': en_transito,
        'total': len(viajeros),
        'maximo': max((f['total'] for f in filas), default=0),
    }


def fecha_pedida(crudo):
    """Read the date from the query string, falling back to today.

    An unreadable date shows today rather than an error: the parameter is a
    convenience on a report, and refusing to draw the panel over a typo helps
    nobody.
    """
    if not crudo:
        return utcnow().date()
    try:
        return date.fromisoformat(str(crudo).strip())
    except (TypeError, ValueError):
        return utcnow().date()


# ======================================================================
# At one moment
# ======================================================================
#
# The day view answers «where does everybody sleep tonight». It cannot answer
# «how many people do we have on the move right now»: a traveller flying that
# day is drawn at the city they fly to, so on the map a flight is its two ends
# and never the journey. The moment view places each person where they are at
# one instant -- in a city, or somewhere along the leg they are on.

def zona_de(actor):
    """The zone hours are read in: the viewer's own, or the installation's."""
    from app.services import settings_service

    return getattr(actor, 'zona_horaria', None) or settings_service.zona_horaria_por_defecto()


def hora_pedida(crudo):
    """``HH`` or ``HH:MM`` from the query string, or None for the day view.

    An unreadable hour falls back to the day view rather than an error, as an
    unreadable date falls back to today.
    """
    if not crudo:
        return None
    texto = str(crudo).strip()
    try:
        if ':' in texto:
            horas, minutos = (int(p) for p in texto.split(':', 1))
        else:
            horas, minutos = int(texto), 0
        return time(horas, minutos)
    except (TypeError, ValueError):
        return None


def instante_de(dia, hora, zona):
    """The UTC instant of ``hora`` on ``dia`` as a clock in ``zona`` reads it."""
    from app.utils.timeutil import to_utc

    return to_utc(datetime.combine(dia, hora), zona)


def _tramos_de(trip_ids):
    """Every dated leg of these trips, earliest first."""
    return (
        TravelSegment.query.filter(
            TravelSegment.trip_id.in_(trip_ids),
            TravelSegment.is_deleted.is_(False),
            TravelSegment.salida_utc.isnot(None),
            TravelSegment.llegada_utc.isnot(None),
        )
        .order_by(TravelSegment.salida_utc.asc())
        .all()
    )


def _suyos(filas, viajero):
    """The rows that speak for this traveller: their own, or the whole trip's."""
    return [
        f for f in filas
        if f.trip_id == viajero.trip_id
        and f.trip_traveler_id in (None, viajero.id)
    ]


def _en_marcha(tramo, instante):
    return as_aware_utc(tramo.salida_utc) <= instante < as_aware_utc(tramo.llegada_utc)


def _extremo(tramo, lado):
    """``(ciudad, pais)`` of one end of a leg, as the catalogue spells it."""
    return ciudad_del_catalogo(
        codigo=getattr(tramo, f'{lado}_codigo'),
        location_id=getattr(tramo, f'{lado}_location_id'),
        nombres=(getattr(tramo, f'{lado}_ciudad'), getattr(tramo, f'{lado}_nombre')),
        pais=getattr(tramo, f'{lado}_pais'),
    )


def punto_intermedio(origen, destino, fraccion):
    """Where along a leg somebody is, as ``(latitud, longitud)``.

    Straight on the drawing rather than along the great circle: the map is
    equirectangular, so a straight line is what the eye follows from one city
    to the other, and a marker off that line would read as a different route.
    A leg across the antimeridian goes the short way round, the way it flies.
    """
    (lat1, lon1), (lat2, lon2) = origen, destino
    if lon2 - lon1 > 180:
        lon2 -= 360
    elif lon1 - lon2 > 180:
        lon2 += 360
    lat = lat1 + (lat2 - lat1) * fraccion
    lon = lon1 + (lon2 - lon1) * fraccion
    lon = (lon + 180) % 360 - 180
    return round(lat, 4), round(lon, 4)


def _situar_a_las(viajero, instante, tramos, alojamientos, destinos):
    """Where one traveller is at ``instante``.

    In this order, each one stronger than the next: on a leg that is under
    way; in the lodging that covers the moment; where the last leg that had
    landed took them; where the next leg leaves from, because somebody who has
    not set off yet is still there; and the trip's declared destination.

    The fourth goes above the fifth here, unlike in the day view. A day is
    mostly spent at the destination, so «the destination» is the better guess
    for a whole day; at eight in the morning before a ten o'clock flight, it
    says somebody has arrived who is still at home.

    Returns ``(origen, datos)``, where ``datos`` is the leg for ``en_transito``
    and ``(ciudad, pais)`` otherwise; ``(None, None)`` when nothing says.
    """
    propios = _suyos(tramos, viajero)

    en_marcha = next((t for t in propios if _en_marcha(t, instante)), None)
    if en_marcha is not None:
        return 'en_transito', en_marcha

    for f in _suyos(alojamientos, viajero):
        if as_aware_utc(f.check_in_utc) <= instante < as_aware_utc(f.check_out_utc):
            return 'alojamiento', ciudad_del_catalogo(
                location_id=f.location_id, nombres=(f.ciudad,), pais=f.pais,
            )

    llegados = [t for t in propios if as_aware_utc(t.llegada_utc) <= instante]
    if llegados:
        return 'tramo', _extremo(llegados[-1], 'destino')

    pendientes = [t for t in propios if as_aware_utc(t.salida_utc) > instante]
    if pendientes:
        return 'antes_de_salir', _extremo(pendientes[0], 'origen')

    for f in destinos:
        if f.trip_id != viajero.trip_id:
            continue
        if f.inicio_utc is not None and as_aware_utc(f.inicio_utc) > instante:
            continue
        if f.fin_utc is not None and as_aware_utc(f.fin_utc) <= instante:
            continue
        return 'destino', ciudad_del_catalogo(
            location_id=f.location_id, nombres=(f.ciudad,), pais=f.pais_codigo,
        )

    return None, None


def donde_estan_a_las(actor, instante):
    """Travellers at one instant: in a city, or along the leg they are on.

    The same scoping, the same completeness rule as :func:`donde_estan`: who
    cannot be placed is listed, never dropped, so ``total`` always equals the
    people in cities plus those on legs plus those unplaced.
    """
    instante = as_aware_utc(instante)
    vacio = {'instante': instante, 'ciudades': [], 'trayectos': [], 'sin_situar': [],
             'sin_ubicacion': [], 'en_transito': [], 'total': 0, 'maximo': 0}

    # A window that contains the instant: open on the right by a microsecond.
    viajeros = _viajeros_en_curso(actor, instante, instante + timedelta(microseconds=1))
    if not viajeros:
        return vacio

    trip_ids = list({d['viaje'].id for d in viajeros.values()})
    tramos = _tramos_de(trip_ids)
    alojamientos = Accommodation.query.filter(
        Accommodation.trip_id.in_(trip_ids),
        Accommodation.is_deleted.is_(False),
        Accommodation.check_in_utc.isnot(None),
        Accommodation.check_out_utc.isnot(None),
    ).all()
    destinos = TripDestination.query.filter(
        TripDestination.trip_id.in_(trip_ids),
        TripDestination.es_escala.is_(False),
    ).order_by(TripDestination.orden.asc()).all()

    ciudades, trayectos = {}, {}
    sin_situar, sin_ubicacion, en_transito = [], [], []

    for datos in viajeros.values():
        viajero = datos['viajero']
        persona = {
            'nombre': datos['usuario'].nombre_completo,
            'viaje_id': str(datos['viaje'].id),
            'viaje': datos['viaje'].referencia,
        }
        origen, situacion = _situar_a_las(viajero, instante, tramos, alojamientos, destinos)

        if origen is None:
            sin_ubicacion.append(persona)
            continue
        persona['segun'] = origen

        if origen == 'en_transito':
            tramo = situacion
            persona['llega'] = tramo.llegada_utc
            en_transito.append(persona)
            desde, hasta = _extremo(tramo, 'origen'), _extremo(tramo, 'destino')
            p1, p2 = coordenadas_de(*desde), coordenadas_de(*hasta)
            if p1 is None or p2 is None:
                ciudad = hasta[0] if p2 is None else desde[0]
                sin_situar.append({**persona, 'ciudad': ciudad, 'pais': None})
                continue
            entrada = trayectos.setdefault(tramo.id, _trayecto(tramo, desde, hasta,
                                                                p1, p2, instante))
            entrada['personas'].append(persona)
            continue

        ciudad, pais = situacion
        punto = coordenadas_de(ciudad, pais)
        if punto is None:
            sin_situar.append({**persona, 'ciudad': ciudad, 'pais': pais})
            continue
        clave = (_sin_acentos(ciudad), (pais or '').upper())
        entrada = ciudades.setdefault(clave, {
            'ciudad': ciudad, 'pais': pais,
            'latitud': punto[0], 'longitud': punto[1], 'personas': [],
        })
        entrada['personas'].append(persona)

    filas = sorted(ciudades.values(),
                   key=lambda c: (-len(c['personas']), _sin_acentos(c['ciudad'])))
    for fila in filas:
        fila['total'] = len(fila['personas'])
        fila['x'], fila['y'] = proyectar(fila['latitud'], fila['longitud'])
    lineas = sorted(trayectos.values(), key=lambda t: t['salida'])
    for linea in lineas:
        linea['total'] = len(linea['personas'])

    return {
        'instante': instante,
        'ciudades': filas,
        'trayectos': lineas,
        'sin_situar': sin_situar,
        'sin_ubicacion': sin_ubicacion,
        'en_transito': en_transito,
        'total': len(viajeros),
        'maximo': max((f['total'] for f in filas), default=0),
    }


def _trayecto(tramo, desde, hasta, p1, p2, instante):
    """One leg under way, ready to be drawn: both ends and the point between."""
    salida, llegada = as_aware_utc(tramo.salida_utc), as_aware_utc(tramo.llegada_utc)
    fraccion = (instante - salida) / (llegada - salida)
    lat, lon = punto_intermedio(p1, p2, fraccion)
    x1, y1 = proyectar(*p1)
    x2, y2 = proyectar(*p2)
    # Across the antimeridian the far end is moved a whole map width, so the
    # line leaves by the near edge; ``cruza`` is the shift that draws it again
    # coming in from the other edge, or the half that reaches the far city
    # would be clipped away.
    cruza = 0
    if x2 - x1 > 50:
        x2 -= 100
        cruza = 100
    elif x1 - x2 > 50:
        x2 += 100
        cruza = -100
    x, y = proyectar(lat, lon)
    return {
        'origen': desde[0], 'destino': hasta[0],
        'tipo': str(tramo.tipo) if tramo.tipo else 'vuelo',
        'numero': tramo.numero,
        'salida': salida, 'llegada': llegada,
        'progreso': int(round(fraccion * 100)),
        'x': x, 'y': y, 'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
        'cruza': cruza,
        'personas': [],
    }


def franja_del_dia(actor, dia, zona):
    """People away and people on the move at each hour of ``dia``, in ``zona``.

    What «en cada momento» needs: one glance at the day instead of
    twenty-four maps. Read on the hour, the same instant the map draws when
    the hour is chosen, so a bar and the map it links to never disagree.

    Two queries for the whole day, not twenty-four calls of the map.
    """
    horas = [instante_de(dia, time(h, 0), zona) for h in range(24)]
    primero, ultimo = horas[0], horas[-1] + timedelta(microseconds=1)

    viajeros = _viajeros_en_curso(actor, primero, ultimo)
    tramos = _tramos_de(list({d['viaje'].id for d in viajeros.values()})) if viajeros else []

    franja = []
    for hora, instante in enumerate(horas):
        fuera = moviendose = 0
        for datos in viajeros.values():
            viajero, viaje = datos['viajero'], datos['viaje']
            desde = _mayor(viajero.desde_utc, viaje.inicio_utc)
            hasta = _menor(viajero.hasta_utc, viaje.fin_utc)
            if (desde is not None and desde > instante) or \
                    (hasta is not None and hasta <= instante):
                continue
            fuera += 1
            if any(_en_marcha(t, instante) for t in _suyos(tramos, viajero)):
                moviendose += 1
        franja.append({'hora': hora, 'etiqueta': f'{hora:02d}',
                       'fuera': fuera, 'en_camino': moviendose})

    maximo = max((f['fuera'] for f in franja), default=0)
    for f in franja:
        f['alto'] = round(100 * f['fuera'] / maximo) if maximo else 0
        f['alto_camino'] = round(100 * f['en_camino'] / maximo) if maximo else 0
    return {'horas': franja, 'maximo': maximo, 'zona': zona,
            'en_camino_max': max((f['en_camino'] for f in franja), default=0)}
