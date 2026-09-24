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
    never appears: the lodging covering that night; failing that, the
    destination of the last leg that had landed; failing that, the trip's
    declared destination; and last, where their next leg departs from.
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
