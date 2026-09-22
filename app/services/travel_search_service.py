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
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qsl

import httpx

from app.extensions import db
from app.services import settings_service
from app.utils import http
from app.utils.errors import AppError

logger = logging.getLogger(__name__)

ENDPOINT = 'https://serpapi.com/search'

#: Long enough for a deep search, short enough that a hung connector does not
#: hold a request open until Gunicorn's own timeout kills it.
TIMEOUT = 25.0

#: Asking where a flight can be bought is a different animal: the connector
#: goes out to sixteen sellers for live prices, and an uncached call took more
#: than the 25 seconds above -- which showed up as «el buscador no responde»
#: for a request that was working fine. Still well inside Gunicorn's 120.
TIMEOUT_COMPRA = 60.0

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


def _pedir(actor, motor, parametros, timeout=TIMEOUT):
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
        with http.cliente(timeout=timeout) as client:
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
#: Currency codes worth a symbol. Anything else is shown as its code, which
#: is ugly but never wrong -- inventing a symbol for a currency we do not know
#: is how a price ends up looking like a different amount.
_SIMBOLOS = {'EUR': '€', 'USD': '$', 'GBP': '£', 'CHF': 'CHF', 'JPY': '¥'}


def _precio(valor):
    """Format a bare number as a price in the configured currency.

    The flights engine returns ``63`` and the hotels engine returns ``«€66»``:
    one hands over a number, the other a string Google already formatted. Only
    the first needs this, and it needs it here rather than in the template
    because this is where the currency is known.
    """
    if valor in (None, ''):
        return None
    if isinstance(valor, str):
        return valor

    codigo = (settings_service.get('BUSQUEDA_VIAJES_MONEDA', 'EUR') or 'EUR').upper()
    simbolo = _SIMBOLOS.get(codigo, codigo)
    # Spanish convention: the amount, a space, then the symbol.
    return f'{valor:,.0f}'.replace(',', '.') + f' {simbolo}'


def _minutos_a_texto(minutos):
    if not minutos:
        return None
    horas, resto = divmod(int(minutos), 60)
    return f'{horas} h {resto:02d} min' if horas else f'{resto} min'


def _instante(texto):
    """Split Google's «2026-09-28 10:50» into the date and the time.

    The screen shows eight options for one day, and repeating the date on both
    ends of every leg turns the one thing that distinguishes them -- the time
    -- into the smallest part of the line. Splitting it here rather than in the
    template is what lets the template align times in a column.

    Returns ``(fecha, hora)``, either of which may be None: an unparseable
    value falls back to showing whatever the connector sent, because an option
    with an odd timestamp is still an option.
    """
    if not texto:
        return None, None
    try:
        momento = datetime.strptime(str(texto).strip(), '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None, str(texto)
    return momento.date(), momento.strftime('%H:%M')


def _tramo(bruto):
    """One leg, reduced to what a manager reads before choosing."""
    salida = bruto.get('departure_airport') or {}
    llegada = bruto.get('arrival_airport') or {}
    fecha_salida, hora_salida = _instante(salida.get('time'))
    fecha_llegada, hora_llegada = _instante(llegada.get('time'))
    return {
        'numero': bruto.get('flight_number'),
        'aerolinea': bruto.get('airline'),
        'avion': bruto.get('airplane'),
        'origen': salida.get('id'),
        'origen_nombre': salida.get('name'),
        'salida': salida.get('time'),
        'fecha_salida': fecha_salida,
        'hora_salida': hora_salida,
        'destino': llegada.get('id'),
        'destino_nombre': llegada.get('name'),
        'llegada': llegada.get('time'),
        'fecha_llegada': fecha_llegada,
        'hora_llegada': hora_llegada,
        'duracion': _minutos_a_texto(bruto.get('duration')),
        'clase': bruto.get('travel_class'),
    }


def _aerolineas(tramos):
    """The operating airlines, in order and without repeating one.

    A connection flown end to end by the same carrier reads as one name, which
    is also the thing that decides whether the bags are through-checked.
    """
    vistas = []
    for tramo in tramos:
        nombre = tramo.get('aerolinea')
        if nombre and nombre not in vistas:
            vistas.append(nombre)
    return vistas


def _dias_de_diferencia(tramos):
    """Nights crossed between taking off and landing.

    Shown as «+1» beside the arrival time. Without it a flight that lands at
    00:35 looks like the fastest one on the page.
    """
    if not tramos:
        return 0
    primera = tramos[0].get('fecha_salida')
    ultima = tramos[-1].get('fecha_llegada')
    if not primera or not ultima:
        return 0
    return (ultima - primera).days


def _opcion_de_vuelo(bruto):
    escalas = [
        {
            'aeropuerto': e.get('name') or e.get('id'),
            'espera': _minutos_a_texto(e.get('duration')),
            'cambio_de_aeropuerto': bool(e.get('overnight')) or None,
        }
        for e in (bruto.get('layovers') or [])
    ]
    tramos = [_tramo(t) for t in (bruto.get('flights') or [])]
    precio = bruto.get('price')
    return {
        'tramos': tramos,
        'escalas': escalas,
        'duracion_total': _minutos_a_texto(bruto.get('total_duration')),
        'duracion_minutos': bruto.get('total_duration') or None,
        'precio': _precio(precio),
        # The number behind the formatted price, kept so the page can sort and
        # compare without parsing back the string it just produced.
        'precio_valor': precio if isinstance(precio, (int, float)) else None,
        'tipo': bruto.get('type'),
        'emisiones_kg': ((bruto.get('carbon_emissions') or {}).get('this_flight') or 0) // 1000
        or None,
        'aerolineas': _aerolineas(tramos),
        'dias_extra': _dias_de_diferencia(tramos),
        'hora_salida': tramos[0]['hora_salida'] if tramos else None,
        'hora_llegada': tramos[-1]['hora_llegada'] if tramos else None,
        'origen': tramos[0]['origen'] if tramos else None,
        'destino': tramos[-1]['destino'] if tramos else None,
        'fecha_salida': tramos[0]['fecha_salida'] if tramos else None,
    }


#: How Google labels the price it found against its own history.
_NIVELES = {
    'low': ('bajo', 'Por debajo de lo habitual en esta ruta.'),
    'typical': ('normal', 'En la horquilla habitual de esta ruta.'),
    'high': ('alto', 'Por encima de lo habitual en esta ruta.'),
}


def _historico_de_precios(bruto):
    """Google's price history for this route, as points ready to draw.

    It arrives in the same answer as the flights, so drawing it costs no extra
    search. That is the whole reason it is drawn here instead of linked: a link
    to Google's own graph looks like the same thing and is a second page, a
    second load and a figure nobody can line up against the list underneath.
    """
    puntos = []
    for entrada in (bruto or []):
        try:
            momento, precio = entrada[0], entrada[1]
            puntos.append({
                'fecha': datetime.fromtimestamp(int(momento), tz=UTC).date(),
                'valor': float(precio),
                # Formatted here like every other price on the page: the
                # currency is known in this module and nowhere else.
                'texto': _precio(round(float(precio))),
            })
        except (TypeError, ValueError, IndexError):
            continue
    return puntos


def _analisis_de_precios(bruto):
    """What the engine says about the price level, or None if it said nothing.

    None rather than an empty shell: a graph with no data drawn as an empty
    frame says «there is no price history», and what it actually means is that
    nobody asked.
    """
    if not bruto:
        return None

    horquilla = bruto.get('typical_price_range') or []
    nivel, explicacion = _NIVELES.get(bruto.get('price_level'), (None, None))
    historico = _historico_de_precios(bruto.get('price_history'))

    if not historico and nivel is None:
        return None

    return {
        'nivel': nivel,
        'explicacion': explicacion,
        'mas_bajo': _precio(bruto.get('lowest_price')),
        'tipico_min': _precio(horquilla[0]) if len(horquilla) > 1 else None,
        'tipico_max': _precio(horquilla[1]) if len(horquilla) > 1 else None,
        'historico': historico,
        'maximo': max((p['valor'] for p in historico), default=0),
        'minimo': min((p['valor'] for p in historico), default=0),
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

    # The parameters travel back with each option because asking where a
    # flight can be bought means repeating the whole search with its token
    # attached; SerpApi will not take the token on its own.
    for opcion, bruta in zip(opciones, brutas, strict=False):
        opcion['token'] = bruta.get('booking_token')
        # Only present on a round trip: it is what the engine wants back to
        # say which returns combine with *this* outbound.
        opcion['token_vuelta'] = bruta.get('departure_token')
        opcion['busqueda'] = parametros

    _registrar(actor, 'google_flights', parametros, len(opciones))
    return {
        'opciones': opciones,
        'precios': _analisis_de_precios(datos.get('price_insights')),
        # Free, and already in the response: one link to the same search on
        # Google Flights. Per-flight purchase links cost another search each,
        # so this is what every result gets without spending anything.
        'enlace': (datos.get('search_metadata') or {}).get('google_flights_url'),
    }


def vuelos_de_vuelta(actor, token, busqueda):
    """The returns that combine with one chosen outbound.

    On a round trip the engine answers with outbound journeys only, each
    carrying a ``departure_token``; the returns are a second search. Like
    :func:`opciones_de_compra` it is billed, so it happens when somebody asks
    for it rather than for the eight results of every planning.

    The price on each option is the **whole trip**, not the return leg: that is
    what the engine reports and the screen has to say so, because a figure that
    could mean either is worse than no figure.
    """
    datos = _pedir(
        actor, 'google_flights', {**(busqueda or {}), 'departure_token': token},
    )

    brutas = (datos.get('best_flights') or []) + (datos.get('other_flights') or [])
    opciones = [_opcion_de_vuelo(o) for o in brutas[:8]]
    for opcion, bruta in zip(opciones, brutas, strict=False):
        opcion['token'] = bruta.get('booking_token')
        opcion['busqueda'] = busqueda or {}

    _registrar(actor, 'google_flights', {**(busqueda or {}), 'vueltas': True},
               len(opciones))
    return {
        'opciones': opciones,
        'enlace': (datos.get('search_metadata') or {}).get('google_flights_url'),
    }


def opciones_de_compra(actor, token, busqueda):
    """Where one flight can actually be bought, and for how much.

    Deliberately on demand. Each call is a second billed search, so doing it
    for the eight results of every planning would spend a day's budget in six
    plannings -- and nobody looks at eight.

    What comes back is not a link but a form submission: Google hands over a
    URL plus a body, so the template posts it. An anchor would silently drop
    the body and land on a page that has lost the flight.
    """
    datos = _pedir(
        actor, 'google_flights', {**(busqueda or {}), 'booking_token': token},
        timeout=TIMEOUT_COMPRA,
    )

    salidas = []
    for bruta in (datos.get('booking_options') or [])[:12]:
        oferta = bruta.get('together') or bruta
        peticion = oferta.get('booking_request') or {}
        if not peticion.get('url'):
            continue
        salidas.append({
            'vendedor': oferta.get('book_with'),
            'operado_por': oferta.get('marketed_as'),
            'precio': _precio(oferta.get('price')),
            'url': peticion.get('url'),
            # Parsed here rather than in the template: the body arrives
            # url-encoded, and a template splitting it on «&» would send the
            # escapes through literally and lose the flight.
            'campos': dict(parse_qsl(peticion.get('post_data') or '')),
        })

    _registrar(actor, 'google_flights_compra', {'ruta': (busqueda or {}).get('departure_id')},
               len(salidas))
    return salidas


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
    resultado = {'vuelos': [], 'alojamiento': [], 'avisos': [],
                 'enlace_vuelos': None, 'consultado': False,
                 # With a return date the engine answers with outbound
                 # journeys and prices the whole trip, so the screen has to
                 # say both things rather than let «222 €» mean either.
                 'hay_vuelta': bool(vuelta), 'fecha_ida': ida,
                 'precios': None}

    if not esta_configurada() or not ida:
        return resultado

    codigo_origen = codigo_de_aeropuerto(origen)
    codigo_destino = codigo_de_aeropuerto(destino)

    if codigo_origen and codigo_destino and codigo_origen != codigo_destino:
        try:
            encontrado = buscar_vuelos(
                actor, codigo_origen, codigo_destino, ida, vuelta, viajeros,
            )
            resultado['vuelos'] = encontrado['opciones']
            resultado['enlace_vuelos'] = encontrado['enlace']
            resultado['precios'] = encontrado['precios']
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
