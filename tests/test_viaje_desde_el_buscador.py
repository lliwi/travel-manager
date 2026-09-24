"""Crear un viaje en borrador con lo que se elige en el planificador.

Lo que esto no puede hacer nunca es parecer una reserva. El conector lee
salidas reales, pero no reserva nada y sus precios son los de Google en ese
momento; si los datos entran en el itinerario como cualquier otro, el viaje
queda indistinguible de uno con billetes comprados.

De ahí las tres reglas que se comprueban aquí: el viaje nace en borrador, cada
campo queda con procedencia «buscador» —ni «manual», que diría que alguien
tecleó ese horario, ni «documento», que exige un texto donde aparezca— y por
debajo del umbral, de modo que sale marcado para confirmar.
"""
import pytest

from app.models.enums import ProvenanceOrigin, TripStatus
from app.models.itinerary import Accommodation, TravelSegment
from app.services import trip_from_search_service as desde_buscador

pytestmark = pytest.mark.unit

VUELO = {
    'tramos': [
        {'numero': 'VY 8250', 'aerolinea': 'Vueling',
         'origen': 'BCN', 'origen_ciudad': 'Barcelona',
         'destino': 'LHR', 'destino_ciudad': 'London',
         'salida': '2026-10-15 10:50', 'llegada': '2026-10-15 12:50'},
    ],
    'precio': '222 €',
}

HOTEL = {'nombre': 'Zedwell Piccadilly Circus', 'ciudad': 'London', 'pais': 'GB',
         'precio_noche': '148 €'}


class TestElViajeQueSeCrea:
    def test_nace_en_borrador(self, app, seeded, gestor):
        """Nada de esto es una reserva, así que no puede nacer confirmado."""
        trip, _ = desde_buscador.crear_viaje(gestor, 'BCN → LON', vuelos=[VUELO])

        assert trip.estado == TripStatus.BORRADOR

    def test_sin_nada_marcado_se_niega(self, app, seeded, gestor):
        from app.utils.errors import ValidationError

        with pytest.raises(ValidationError):
            desde_buscador.crear_viaje(gestor, 'x')

    def test_escribe_el_tramo_con_sus_datos(self, app, seeded, gestor):
        trip, _ = desde_buscador.crear_viaje(gestor, 'BCN → LON', vuelos=[VUELO])
        tramo = TravelSegment.query.filter_by(trip_id=trip.id).one()

        assert tramo.numero == 'VY 8250'
        assert tramo.origen_codigo == 'BCN'
        assert tramo.destino_codigo == 'LHR'

    def test_escribe_el_alojamiento(self, app, seeded, gestor):
        from datetime import datetime

        trip, _ = desde_buscador.crear_viaje(
            gestor, 'BCN → LON', alojamientos=[HOTEL],
            entrada=datetime(2026, 10, 15), salida=datetime(2026, 10, 18),
        )
        hotel = Accommodation.query.filter_by(trip_id=trip.id).one()

        assert hotel.nombre == 'Zedwell Piccadilly Circus'
        assert hotel.check_in_utc is not None

    def test_las_fechas_del_viaje_salen_del_itinerario(self, app, seeded, gestor):
        """Preguntarlas otra vez sería pedir dos veces lo que ya se sabe."""
        trip, _ = desde_buscador.crear_viaje(gestor, 'BCN → LON', vuelos=[VUELO])

        assert trip.inicio_local is not None
        assert trip.inicio_tz == 'Europe/Madrid'
        assert trip.fin_tz == 'Europe/London'


class TestQueNadieLoConfundaConUnaReserva:
    def test_la_procedencia_es_el_buscador(self, app, seeded, gestor):
        """Ni «manual» —nadie tecleó ese horario— ni «documento», que exige un
        texto donde el valor aparezca."""
        from app.models.itinerary import FieldProvenance

        trip, _ = desde_buscador.crear_viaje(gestor, 'BCN → LON', vuelos=[VUELO])
        tramo = TravelSegment.query.filter_by(trip_id=trip.id).one()
        origenes = {
            str(f.origen) for f in
            FieldProvenance.query.filter_by(entidad_id=tramo.id)
        }

        assert origenes == {str(ProvenanceOrigin.BUSCADOR)}

    def test_queda_marcado_para_confirmar(self, app, seeded, gestor):
        trip, _ = desde_buscador.crear_viaje(gestor, 'BCN → LON', vuelos=[VUELO])
        tramo = TravelSegment.query.filter_by(trip_id=trip.id).one()

        assert tramo.requiere_revision is True

    def test_cada_campo_escrito_tiene_su_procedencia(self, app, seeded, gestor):
        """Un campo sin fila de procedencia parece tecleado por una persona."""
        from app.models.itinerary import FieldProvenance

        trip, _ = desde_buscador.crear_viaje(gestor, 'BCN → LON', vuelos=[VUELO])
        tramo = TravelSegment.query.filter_by(trip_id=trip.id).one()
        campos = {
            f.campo for f in FieldProvenance.query.filter_by(entidad_id=tramo.id)
        }

        assert campos == set(desde_buscador.CAMPOS_TRAMO)

    def test_sale_en_lo_pendiente_de_confirmar(self, app, seeded, gestor):
        from app.services import itinerary_service

        trip, _ = desde_buscador.crear_viaje(gestor, 'BCN → LON', vuelos=[VUELO])

        assert itinerary_service.pendientes_de_confirmar(gestor, trip)


class TestLasHorasNoSeInventan:
    def test_la_zona_sale_del_catalogo(self, app, seeded, gestor):
        trip, avisos = desde_buscador.crear_viaje(gestor, 'x', vuelos=[VUELO])
        tramo = TravelSegment.query.filter_by(trip_id=trip.id).one()

        assert tramo.salida_tz == 'Europe/Madrid'
        assert tramo.llegada_tz == 'Europe/London'
        assert avisos == []

    def test_un_sitio_desconocido_se_avisa_en_vez_de_callarse(self, app, seeded, gestor):
        """«set_instant» cae a UTC cuando no hay zona, y la tripleta queda
        completa y creíble. Decirlo es lo único que impide que una hora leída
        como UTC pase por buena: un desfase equivocado corrompe en silencio
        todos los márgenes que el motor de alertas calcula después."""
        vuelo = {'tramos': [{**VUELO['tramos'][0], 'destino': 'ZZZ',
                             'destino_ciudad': 'Ciudad Inexistente'}]}

        trip, avisos = desde_buscador.crear_viaje(gestor, 'x', vuelos=[vuelo])
        tramo = TravelSegment.query.filter_by(trip_id=trip.id).one()

        assert tramo.llegada_tz == 'UTC'
        assert any('UTC' in a for a in avisos)
        assert tramo.requiere_revision is True

    def test_una_hora_ilegible_no_tumba_la_creacion(self, app, seeded, gestor):
        vuelo = {'tramos': [{**VUELO['tramos'][0], 'salida': 'mañana'}]}

        trip, _ = desde_buscador.crear_viaje(gestor, 'x', vuelos=[vuelo])
        tramo = TravelSegment.query.filter_by(trip_id=trip.id).one()

        assert tramo.salida_local is None
        assert tramo.destino_codigo == 'LHR'

    def test_el_nombre_en_ingles_se_reconoce_igual(self, app, seeded, gestor):
        """El hotel llega como «London» porque así lo dijo la reserva."""
        from datetime import datetime

        trip, avisos = desde_buscador.crear_viaje(
            gestor, 'x', alojamientos=[HOTEL],
            entrada=datetime(2026, 10, 15), salida=datetime(2026, 10, 18),
        )
        hotel = Accommodation.query.filter_by(trip_id=trip.id).one()

        assert hotel.check_in_tz == 'Europe/London'
        assert avisos == []


@pytest.mark.integration
class TestLaPantalla:
    def test_crea_el_viaje_y_lleva_a_el(self, as_user, gestor, seeded):
        import json

        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar/crear', data={
                'titulo': 'BCN → LON',
                'vuelo': json.dumps(VUELO),
                'entrada': '2026-10-15',
            })

        assert respuesta.status_code == 302
        assert '/trips/' in respuesta.headers['Location']

    def test_sin_marcar_nada_vuelve_y_lo_dice(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar/crear', data={},
                                    follow_redirects=True)

        assert 'Marque al menos' in respuesta.get_data(as_text=True)

    def test_una_seleccion_ilegible_no_rompe_la_pantalla(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar/crear',
                                    data={'vuelo': 'esto no es json'},
                                    follow_redirects=True)

        assert respuesta.status_code == 200
        assert 'Marque al menos' in respuesta.get_data(as_text=True)

    def test_solo_quien_puede_crear_viajes(self, as_user, viajero, seeded):
        import json

        with as_user(viajero) as client:
            respuesta = client.post('/trips/planificar/crear',
                                    data={'vuelo': json.dumps(VUELO)})

        assert respuesta.status_code == 403
