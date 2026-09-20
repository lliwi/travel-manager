"""Real flight and lodging options, from SerpApi's Google Travel engines.

Until now the planning assistant could say *how* to travel and never *what*
exists: the application had no availability connector, so a model asked for
«el mejor vuelo» would answer with a flight number and a price that came from
nowhere, and somebody would act on it. This connector is what makes the
difference between orienting and informing.

Three things it is important to be honest about, because they shape the design:

- **It reads Google Flights and Google Hotels; it is not a GDS.** Prices are
  what Google was showing, which is indicative and can be stale by the time
  somebody books. The same caveat as looking at Google Flights yourself.
- **Nothing here books anything.** SerpApi returns a `booking_token` that leads
  to booking options elsewhere. The assistant still never claims a reservation
  exists, which was already the rule.
- **There is no train engine.** Renfe, SNCF and the rest are not covered. For
  a rail journey the assistant keeps doing what it did before, and says so
  rather than pretending the absence of results means no trains run.

What leaves the perimeter is a route, some dates and a headcount -- no names,
no documents. It still only leaves because an administrator put a key in
Ajustes, which is the explicit decision the project asks for.
"""

import logging
from datetime import date, timedelta

import httpx

from app.extensions import db
from app.services import settings_service
from app.utils.errors import AppError

logger = logging.getLogger(__name__)

ENDPOINT = 'https://serpapi.com/search'

#: Long enough for a deep search, short enough that a hung connector does not
#: hold a request open until Gunicorn's own timeout kills it.
TIMEOUT = 25.0

#: Audit action every search is recorded under. It is also what the daily
#: budget is counted from, so the two can never disagree.
ACCION = 'busqueda_viajes.consulta'


class BusquedaNoDisponible(AppError):
    """The connector cannot answer, for a reason worth showing a person."""

    codigo = 'busqueda_no_disponible'
    status = 503


def esta_configurada():
    """True when an administrator enabled the connector and supplied a key.

    Both, not either: the switch on its own would produce a confusing failure
    on every search, and a key on its own should not start spending money
    because somebody pasted it while evaluating.
    """
    return bool(
        settings_service.get_bool('BUSQUEDA_VIAJES_HABILITADA', False)
        and settings_service.get('BUSQUEDA_VIAJES_API_KEY')
    )


def estado():
    """Why the connector is or is not answering, in one word.

    ``esta_configurada`` answers yes or no, which is all the search path needs.
    The screen needs the reason: a key that is present but switched off looks
    exactly like no key at all, and somebody who has just pasted one deserves
    to be told which of the two halves is missing rather than left with an
    empty result and no explanation.
    """
    if not settings_service.get('BUSQUEDA_VIAJES_API_KEY'):
        return 'sin_clave'
    if not settings_service.get_bool('BUSQUEDA_VIAJES_HABILITADA', False):
        return 'desactivado'
    if presupuesto_restante() <= 0:
        return 'sin_presupuesto'
    return 'activo'


def _consumo_de_hoy():
    """Searches already billed today, counted from the audit trail.

    Counted from what was recorded rather than from a counter in memory: four
    Gunicorn workers would keep four counters, and the budget would be whatever
    the busiest worker had seen.
    """
    from app.models.audit import AuditEvent
    from app.utils import timeutil

    desde = timeutil.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.session.query(db.func.count(AuditEvent.id))
        .filter(
            AuditEvent.accion == ACCION,
            AuditEvent.created_at >= desde,
        )
        .scalar()
        or 0
    )


def presupuesto_restante():
    """How many more searches today's budget allows. Negative is impossible."""
    limite = settings_service.get_int('BUSQUEDA_VIAJES_MAXIMAS_DIARIAS', 50)
    return max(0, limite - _consumo_de_hoy())


def _registrar(actor, motor, parametros, resultados, error=None):
    """Record the search: each one costs money and somebody pays the bill.

    It is also what the daily budget is counted from, so this has to happen on
    the failures too -- a connector erroring in a loop still bills.
    """
    from app.models.enums import AuditResult
    from app.services import audit_service

    audit_service.record(
        accion=ACCION,
        actor=actor,
        resultado=AuditResult.ERROR if error else AuditResult.EXITO,
        metadatos={
            'motor': motor,
            # The route and the dates, never a traveller. Enough to explain an
            # invoice, not enough to rebuild who went where.
            'parametros': {k: v for k, v in parametros.items() if k != 'api_key'},
            'resultados': resultados,
            'error': error,
        },
    )


def _pedir(actor, motor, parametros):
    """One call to SerpApi, with the budget and the failures handled here."""
    if not esta_configurada():
        raise BusquedaNoDisponible(
            'La búsqueda de opciones reales no está configurada. Un '
            'administrador puede activarla en Ajustes.'
        )

    if presupuesto_restante() <= 0:
        raise BusquedaNoDisponible(
            'Se ha alcanzado el límite de búsquedas de hoy. El asistente '
            'puede seguir orientando sin opciones concretas.'
        )

    consulta = dict(parametros)
    consulta['engine'] = motor
    consulta['api_key'] = settings_service.get('BUSQUEDA_VIAJES_API_KEY')
    consulta.setdefault('currency', settings_service.get('BUSQUEDA_VIAJES_MONEDA', 'EUR'))
    consulta.setdefault('gl', 'es')
    # Not on hotels. Measured, not assumed: with `hl=es` the hotels engine
    # answers «Google Hotels hasn't returned any results for this query» for a
    # search that returns twenty properties without it. The flights engine is
    # unaffected, and `gl=es` is fine for both. Losing Spanish labels on hotel
    # amenities is a far smaller price than losing every hotel.
    if motor != 'google_hotels':
        consulta.setdefault('hl', 'es')

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            respuesta = client.get(ENDPOINT, params=consulta)
    except httpx.HTTPError as exc:
        # The key must never reach a log line, and httpx puts the full URL --
        # query string included -- in the text of its exceptions.
        logger.warning('El conector de búsqueda no respondió: %s', type(exc).__name__)
        _registrar(actor, motor, parametros, 0, error='sin_respuesta')
        raise BusquedaNoDisponible(
            'El buscador de opciones no responde en este momento.'
        ) from None

    if respuesta.status_code == 401:
        _registrar(actor, motor, parametros, 0, error='clave_rechazada')
        raise BusquedaNoDisponible(
            'El buscador ha rechazado la clave configurada. Revísela en Ajustes.'
        )
    if respuesta.status_code != 200:
        _registrar(actor, motor, parametros, 0, error=f'http_{respuesta.status_code}')
        raise BusquedaNoDisponible('El buscador de opciones devolvió un error.')

    datos = respuesta.json()
    if datos.get('error'):
        _registrar(actor, motor, parametros, 0, error='sin_resultados')
        # Not an exception: «no flights on that date» is an answer, and the
        # assistant should say it rather than look broken.
        return {}

    return datos


# ======================================================================
# Flights
# ======================================================================
def _minutos_a_texto(minutos):
    if not minutos:
        return None
    horas, resto = divmod(int(minutos), 60)
    return f'{horas} h {resto:02d} min' if horas else f'{resto} min'


def _tramo(bruto):
    """One leg, reduced to what a manager reads before choosing."""
    salida = bruto.get('departure_airport') or {}
    llegada = bruto.get('arrival_airport') or {}
    return {
        'numero': bruto.get('flight_number'),
        'aerolinea': bruto.get('airline'),
        'avion': bruto.get('airplane'),
        'origen': salida.get('id'),
        'origen_nombre': salida.get('name'),
        'salida': salida.get('time'),
        'destino': llegada.get('id'),
        'destino_nombre': llegada.get('name'),
        'llegada': llegada.get('time'),
        'duracion': _minutos_a_texto(bruto.get('duration')),
        'clase': bruto.get('travel_class'),
    }


def _opcion_de_vuelo(bruto):
    escalas = [
        {
            'aeropuerto': e.get('name') or e.get('id'),
            'espera': _minutos_a_texto(e.get('duration')),
            'cambio_de_aeropuerto': bool(e.get('overnight')) or None,
        }
        for e in (bruto.get('layovers') or [])
    ]
    return {
        'tramos': [_tramo(t) for t in (bruto.get('flights') or [])],
        'escalas': escalas,
        'duracion_total': _minutos_a_texto(bruto.get('total_duration')),
        'precio': bruto.get('price'),
        'tipo': bruto.get('type'),
        'emisiones_kg': ((bruto.get('carbon_emissions') or {}).get('this_flight') or 0) // 1000
        or None,
    }


def buscar_vuelos(actor, origen, destino, ida, vuelta=None, viajeros=1,
                  clase='economy'):
    """Real flight options between two airports.

    ``origen`` and ``destino`` are IATA codes. Returns an empty list when the
    engine has nothing for those dates, which is information, not a failure.
    """
    _CLASES = {'economy': 1, 'premium': 2, 'business': 3, 'first': 4}

    parametros = {
        'departure_id': (origen or '').upper(),
        'arrival_id': (destino or '').upper(),
        'outbound_date': str(ida),
        'adults': max(1, int(viajeros or 1)),
        'travel_class': _CLASES.get(clase, 1),
        'type': 1 if vuelta else 2,
    }
    if vuelta:
        parametros['return_date'] = str(vuelta)

    datos = _pedir(actor, 'google_flights', parametros)

    brutas = (datos.get('best_flights') or []) + (datos.get('other_flights') or [])
    opciones = [_opcion_de_vuelo(o) for o in brutas[:8]]
    _registrar(actor, 'google_flights', parametros, len(opciones))
    return opciones


# ======================================================================
# Lodging
# ======================================================================
def _alojamiento(bruto):
    tarifa = bruto.get('rate_per_night') or {}
    total = bruto.get('total_rate') or {}
    return {
        'nombre': bruto.get('name'),
        'tipo': bruto.get('type'),
        'valoracion': bruto.get('overall_rating'),
        'opiniones': bruto.get('reviews'),
        'categoria': bruto.get('hotel_class'),
        'precio_noche': tarifa.get('lowest'),
        'precio_total': total.get('lowest'),
        'zona': bruto.get('nearby_places', [{}])[0].get('name')
        if bruto.get('nearby_places') else None,
        'enlace': bruto.get('link'),
    }


def buscar_alojamiento(actor, destino, entrada, salida, viajeros=1):
    """Lodging options in a place between two dates.

    ``destino`` is free text -- a city, a district, «cerca de LHR» -- because
    that is what Google Hotels takes and what a manager would type.
    """
    parametros = {
        'q': destino,
        'check_in_date': str(entrada),
        'check_out_date': str(salida),
        'adults': max(1, int(viajeros or 1)),
    }

    datos = _pedir(actor, 'google_hotels', parametros)

    brutas = datos.get('properties') or []
    opciones = [_alojamiento(o) for o in brutas[:8]]
    _registrar(actor, 'google_hotels', parametros, len(opciones))
    return opciones


def fechas_por_defecto(ida, vuelta):
    """Lodging dates implied by a journey, when nobody gave any.

    A same-day return needs no hotel; anything else needs the nights between
    arriving and leaving.
    """
    if not ida:
        return None, None
    entrada = ida
    salida = vuelta or (
        ida + timedelta(days=1) if isinstance(ida, date) else None
    )
    if salida and salida <= entrada:
        return None, None
    return entrada, salida


# ======================================================================
# Resolving what somebody typed
# ======================================================================
def codigo_de_aeropuerto(texto):
    """The IATA code for a place, or None when the catalogue does not know it.

    The flight engine takes codes, and people type «Barcelona». Resolved from
    our own catalogue rather than guessed: an invented code silently searches
    the wrong route, and a search that returns the wrong city's flights is
    worse than one that returns nothing.
    """
    from app.models.catalog import Location
    from app.models.enums import LocationKind

    if not texto:
        return None

    limpio = str(texto).strip()
    if not limpio:
        return None

    base = Location.query.filter(
        Location.activo.is_(True), Location.tipo == LocationKind.AEROPUERTO
    )

    fila = (
        base.filter(Location.codigo == limpio.upper()).first()
        or base.filter(Location.ciudad.ilike(limpio)).first()
        or base.filter(Location.nombre.ilike(f'%{limpio}%')).first()
    )
    return fila.codigo if fila else None


def buscar_para(actor, origen, destino, ida, vuelta=None, viajeros=1):
    """Everything the connector can say about one journey.

    Returns the options and, separately, what it could *not* answer. The
    second half matters as much as the first: a rail journey comes back with
    no flights, and without an explicit note that trains are not covered, an
    empty list reads as «there is no way to get there».
    """
    resultado = {'vuelos': [], 'alojamiento': [], 'avisos': [], 'consultado': False}

    if not esta_configurada() or not ida:
        return resultado

    codigo_origen = codigo_de_aeropuerto(origen)
    codigo_destino = codigo_de_aeropuerto(destino)

    if codigo_origen and codigo_destino and codigo_origen != codigo_destino:
        try:
            resultado['vuelos'] = buscar_vuelos(
                actor, codigo_origen, codigo_destino, ida, vuelta, viajeros,
            )
            resultado['consultado'] = True
        except BusquedaNoDisponible as error:
            resultado['avisos'].append(error.mensaje)
    else:
        resultado['avisos'].append(
            'No se han buscado vuelos: el catálogo no reconoce un aeropuerto '
            'para uno de los dos extremos. Para trenes y autobuses el buscador '
            'no tiene cobertura, así que lo que sigue es orientación.'
        )

    entrada, salida = fechas_por_defecto(ida, vuelta)
    if entrada and salida:
        try:
            resultado['alojamiento'] = buscar_alojamiento(
                actor, destino, entrada, salida, viajeros,
            )
            resultado['consultado'] = True
        except BusquedaNoDisponible as error:
            resultado['avisos'].append(error.mensaje)

    return resultado
