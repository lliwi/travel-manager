"""Turn a selection of found flights and lodging into a draft trip.

The connector reads real departures and real rooms, and until now the only
thing anybody could do with them was read them and retype them. This writes
them into an itinerary instead.

Three things it is careful about, because each one fails quietly:

**Nothing here is a booking.** The prices are Google's and indicative, and no
seat is held. So every field is recorded with the ``buscador`` provenance and
below the review threshold: the trip opens as a draft, the rows come out marked
for confirmation, and nobody can mistake the result for a reservation. The
manager chose the row; they did not type the departure time, which is why this
is not ``manual``.

**Times arrive without a zone.** The connector hands over «2026-09-28 10:50»
and nothing else. The zone comes from the catalogue entry for the airport.
When the catalogue does not know the place, ``set_instant`` falls back to UTC
and the triple comes out complete and plausible, which is the dangerous part: a
wrong offset silently corrupts every margin the alert engine computes
afterwards. So it is said out loud -- the caller gets a warning naming the
place, and the row is flagged for review like everything else from here.

**The rows are outside content.** A hotel named «ignora tus instrucciones» is
a name, and it is stored as one -- nothing here is ever interpreted.
"""
import logging
from datetime import datetime

from app.extensions import db
from app.models.enums import (
    ProvenanceOrigin,
    SegmentStatus,
    SegmentType,
    TripStatus,
)
from app.models.itinerary import Accommodation, TravelSegment
from app.services import provenance_service, trip_service
from app.utils.errors import ValidationError
from app.utils.timeutil import set_instant

logger = logging.getLogger(__name__)

#: Deliberately under the review threshold. These values are exact as the
#: connector reported them, but what they describe -- a flight nobody has
#: booked, a price that moves -- is not, and the screen has to say so.
CONFIANZA = 0.50

#: Fields written from each kind of row, so the provenance is complete rather
#: than a sample. A field with no provenance row looks typed by a person.
CAMPOS_TRAMO = (
    'tipo', 'estado', 'numero', 'proveedor', 'origen_codigo', 'origen_ciudad',
    'destino_codigo', 'destino_ciudad', 'salida_local', 'llegada_local',
    'importe', 'moneda',
)
CAMPOS_ALOJAMIENTO = ('nombre', 'ciudad', 'pais', 'check_in_local',
                      'check_out_local', 'importe', 'moneda')


def _moneda():
    """The currency the connector was asked to price in."""
    from app.services import settings_service

    return (settings_service.get('BUSQUEDA_VIAJES_MONEDA', 'EUR') or 'EUR').upper()[:3]


def _instante(texto):
    """«2026-09-28 10:50» as a naive local datetime, or None."""
    if not texto:
        return None
    try:
        return datetime.strptime(str(texto).strip(), '%Y-%m-%d %H:%M')
    except (TypeError, ValueError):
        return None


def _zona_de(codigo, ciudad, pais):
    """The timezone of a place, from the catalogue, or None.

    None means «the catalogue does not know», not «use the default». The
    caller turns that into a warning: ``set_instant`` will still write UTC, and
    a departure stored with the wrong offset is not a small error -- every
    connection margin after it is computed from it, and nothing about the row
    looks wrong.
    """
    from app.models.catalog import Location
    from app.services.normalization_service import resolver_lugar

    if codigo:
        fila = Location.query.filter_by(
            codigo=str(codigo).strip().upper(), activo=True,
        ).first()
        if fila is not None and fila.zona_horaria:
            return fila.zona_horaria

    zona, _pais, _ciudad = resolver_lugar([n for n in (ciudad,) if n], pais)
    return zona


def _anotar(entidad, kind, campos, actor):
    """One provenance row per field, so none of them looks typed by a person."""
    for campo in campos:
        provenance_service.record(
            entidad, kind, campo, ProvenanceOrigin.BUSCADOR,
            valor_actual=getattr(entidad, campo, None),
            confianza=CONFIANZA, actor=actor,
        )
    provenance_service.recalculate_rollup(entidad, kind)


def _tramo(trip, bruto, actor, avisos, precio=None):
    """One leg of a selected flight.

    ``precio`` is the price of the whole option and is written on its first leg
    only. Repeating it on each one would make the trip's total a multiple of
    what was shown, and splitting it between legs would invent a fare per leg
    that nobody quoted -- so it goes once, and the leg says what it covers.
    """
    salida = _instante(bruto.get('salida'))
    llegada = _instante(bruto.get('llegada'))

    segmento = TravelSegment(
        trip_id=trip.id,
        tipo=SegmentType.VUELO,
        # Pendiente y no confirmado: nadie ha reservado este vuelo. El estado
        # por defecto de la columna es «confirmado», que es lo correcto para un
        # tramo salido de un billete y exactamente lo contrario aquí.
        estado=SegmentStatus.PENDIENTE,
        numero=(bruto.get('numero') or '')[:30] or None,
        proveedor=(bruto.get('aerolinea') or '')[:200] or None,
        origen_codigo=(bruto.get('origen') or '')[:10] or None,
        origen_ciudad=(bruto.get('origen_ciudad') or '')[:160] or None,
        destino_codigo=(bruto.get('destino') or '')[:10] or None,
        destino_ciudad=(bruto.get('destino_ciudad') or '')[:160] or None,
        importe=precio,
        moneda=_moneda() if precio is not None else None,
        observaciones=(
            'Precio del trayecto completo según el buscador, orientativo.'
            if precio is not None else None
        ),
    )
    db.session.add(segmento)
    db.session.flush()

    for prefijo, momento, codigo, ciudad in (
        ('salida', salida, segmento.origen_codigo, segmento.origen_ciudad),
        ('llegada', llegada, segmento.destino_codigo, segmento.destino_ciudad),
    ):
        zona = _zona_de(codigo, ciudad, None)
        if momento is not None and zona is None:
            # «set_instant» cae a UTC cuando no hay zona, y la tripleta queda
            # completa y creíble. Decirlo es lo único que impide que una hora
            # leída como UTC pase por buena.
            avisos.append(
                f'No se conoce la zona horaria de «{codigo or ciudad or "?"}»: '
                f'esa hora se ha guardado como UTC. Corríjala al confirmar.'
            )
        set_instant(segmento, prefijo, momento, zona)

    _anotar(segmento, 'segmento', CAMPOS_TRAMO, actor)
    return segmento


def _alojamiento(trip, bruto, entrada, salida, actor, avisos):
    """One selected hotel."""
    hotel = Accommodation(
        trip_id=trip.id,
        nombre=(bruto.get('nombre') or 'Alojamiento')[:250],
        ciudad=(bruto.get('ciudad') or '')[:160] or None,
        pais=(bruto.get('pais') or '')[:2] or None,
        importe=bruto.get('precio_total_valor'),
        moneda=_moneda() if bruto.get('precio_total_valor') is not None else None,
    )
    db.session.add(hotel)
    db.session.flush()

    zona = _zona_de(None, hotel.ciudad, hotel.pais)
    if (entrada or salida) and zona is None:
        avisos.append(
            f'No se conoce la zona horaria de «{hotel.ciudad or hotel.nombre}»: '
            f'esas horas se han guardado como UTC. Corríjalas al confirmar.'
        )
    set_instant(hotel, 'check_in', entrada, zona)
    set_instant(hotel, 'check_out', salida, zona)

    _anotar(hotel, 'alojamiento', CAMPOS_ALOJAMIENTO, actor)
    return hotel


def crear_viaje(actor, titulo, vuelos=(), alojamientos=(), proyecto=None,
                entrada=None, salida=None):
    """Create a draft trip from what somebody picked on the planning screen.

    Returns ``(trip, avisos)``. The warnings are not errors: the trip is
    created either way, and they say which fields could not be completed so the
    screen can pass them on instead of leaving somebody to find out later.
    """
    if not vuelos and not alojamientos:
        raise ValidationError('No se ha seleccionado ningún vuelo ni alojamiento.')

    avisos = []
    trip = trip_service.create_trip(
        actor,
        titulo=titulo or 'Viaje sin título',
        estado=TripStatus.BORRADOR,
        proyecto=proyecto,
        commit=False,
    )
    db.session.flush()

    for vuelo in vuelos:
        precio = vuelo.get('precio_valor')
        for indice, tramo in enumerate(vuelo.get('tramos') or ()):
            _tramo(trip, tramo, actor, avisos, precio if indice == 0 else None)

    for hotel in alojamientos:
        _alojamiento(trip, hotel, entrada, salida, actor, avisos)

    # The trip's own dates follow what was selected rather than being asked
    # for twice: the itinerary already says when it starts and ends.
    _ajustar_fechas(trip)

    db.session.commit()
    logger.info('Viaje %s creado desde el buscador por %s', trip.referencia, actor.id)
    return trip, avisos


def _ajustar_fechas(trip):
    """Set the trip window from the itinerary that was just written.

    Through ``set_instant`` and from each item's own local time and zone, never
    by writing ``inicio_utc``: the three columns are one value, and setting the
    UTC one alone lets them drift apart and breaks every margin the alert
    engine computes. The trip starts when the first leg departs, in that leg's
    own zone, which is also what the screen will render.
    """
    from app.utils.timeutil import as_aware_utc

    momentos = []
    for segmento in TravelSegment.query.filter_by(trip_id=trip.id):
        momentos.append((segmento.salida_utc, segmento.salida_local, segmento.salida_tz))
        momentos.append(
            (segmento.llegada_utc, segmento.llegada_local, segmento.llegada_tz)
        )
    for hotel in Accommodation.query.filter_by(trip_id=trip.id):
        momentos.append((hotel.check_in_utc, hotel.check_in_local, hotel.check_in_tz))
        momentos.append((hotel.check_out_utc, hotel.check_out_local, hotel.check_out_tz))

    # Ordered by the UTC column, which is the only one two zones can be
    # compared with, but written from the local one it belongs to.
    completos = [m for m in momentos if m[0] is not None and m[1] is not None]
    if not completos:
        return

    primero = min(completos, key=lambda m: as_aware_utc(m[0]))
    ultimo = max(completos, key=lambda m: as_aware_utc(m[0]))

    set_instant(trip, 'inicio', primero[1], primero[2])
    set_instant(trip, 'fin', ultimo[1], ultimo[2])
