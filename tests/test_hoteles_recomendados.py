"""The hotels the organisation recommends replace any hotel search.

The managers keep the list; the planner offers it, and only it, as lodging --
to the screen and to the model alike. Three ways this goes wrong quietly: a
hotel from outside slips in, a hotel filed under one spelling of a city is not
found under another, and somebody who does not manage edits the list.
"""
from datetime import date

import pytest

from app.models.catalog import HotelRecomendado
from app.services import hotel_recomendado_service, travel_search_service
from app.services.authorization_service import Permiso, can
from app.utils.errors import ValidationError


@pytest.fixture
def carlton(gestor, seeded):
    return hotel_recomendado_service.crear(
        gestor, nombre='Hotel Carlton', ciudad='Lyon', zona='Presqu\'île',
        precio_noche='120', moneda='EUR', enlace='https://carlton.test',
        notas='Tarifa corporativa CORP26.',
    )


class TestLaLista:
    def test_se_crea_se_edita_y_se_retira(self, gestor, carlton):
        hotel_recomendado_service.actualizar(gestor, carlton, precio_noche='135')
        assert float(carlton.precio_noche) == 135

        hotel_recomendado_service.eliminar(gestor, carlton)
        assert hotel_recomendado_service.listar() == []
        assert HotelRecomendado.query.count() == 1, 'se retira, no se borra'

    def test_un_precio_sin_moneda_no(self, gestor, seeded):
        with pytest.raises(ValidationError, match='moneda'):
            hotel_recomendado_service.crear(
                gestor, nombre='Sin moneda', ciudad='Lyon', precio_noche='90',
            )

    def test_un_enlace_que_no_es_web_no(self, gestor, seeded):
        """It is drawn as a link on the planner."""
        with pytest.raises(ValidationError, match='http'):
            hotel_recomendado_service.crear(
                gestor, nombre='Raro', ciudad='Lyon', enlace='javascript:alert(1)',
            )

    def test_queda_en_la_auditoria(self, carlton):
        from app.models.audit import AuditEvent

        assert AuditEvent.query.filter_by(
            accion='recommended_hotel.created').count() == 1

    @pytest.mark.security
    def test_solo_quien_gestiona(self, as_user, admin, gestor, viajero, carlton):
        assert can(gestor, Permiso.GESTIONAR_HOTELES)
        assert can(admin, Permiso.GESTIONAR_HOTELES)
        assert not can(viajero, Permiso.GESTIONAR_HOTELES)

        with as_user(viajero) as client:
            assert client.get('/trips/hoteles-recomendados').status_code == 403
            assert client.post(
                f'/trips/hoteles-recomendados/{carlton.id}/eliminar').status_code == 403
            assert 'Hoteles recomendados' not in client.get('/trips/').get_data(as_text=True)
        assert not carlton.is_deleted


class TestLaPantallaDelGestor:
    def test_alta_y_listado(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            respuesta = client.post('/trips/hoteles-recomendados/nuevo', data={
                'nombre': 'NH Collection', 'ciudad': 'Lyon', 'pais': '',
                'precio_noche': '110', 'moneda': 'EUR', 'activo': 'y',
            })
            html = client.get('/trips/hoteles-recomendados').get_data(as_text=True)

        assert respuesta.status_code == 302
        assert 'NH Collection' in html
        assert 'Hoteles recomendados' in html

    def test_edicion(self, as_user, gestor, carlton):
        with as_user(gestor) as client:
            client.post(f'/trips/hoteles-recomendados/{carlton.id}/editar', data={
                'nombre': 'Hotel Carlton Lyon', 'ciudad': 'Lyon', 'pais': '',
                'precio_noche': '120', 'moneda': 'EUR',
            })

        assert carlton.nombre == 'Hotel Carlton Lyon'
        assert carlton.activo is False, 'la casilla desmarcada lo oculta'


class TestLoQueOfreceElPlanificador:
    def test_los_de_esa_ciudad_con_el_total_de_las_noches(self, gestor, carlton):
        opciones = travel_search_service.buscar_para(
            gestor, 'Barcelona', 'Lyon', date(2026, 12, 1), date(2026, 12, 3),
        )

        (hotel,) = opciones['alojamiento']
        assert hotel['nombre'] == 'Hotel Carlton'
        assert hotel['recomendado'] is True
        assert hotel['precio_noche'] == '120 EUR'
        assert hotel['precio_total_valor'] == 240
        assert hotel['notas'] == 'Tarifa corporativa CORP26.'

    def test_sin_conector_tambien(self, gestor, carlton):
        """This half of the answer never leaves the perimeter."""
        assert travel_search_service.estado() != 'activo'

        opciones = travel_search_service.buscar_para(
            gestor, 'Barcelona', 'Lyon', date(2026, 12, 1), date(2026, 12, 3),
        )

        assert len(opciones['alojamiento']) == 1

    def test_la_ciudad_se_encuentra_escrita_de_otra_forma(self, gestor, seeded):
        hotel_recomendado_service.crear(gestor, nombre='The Hoxton', ciudad='London')

        for escrito in ('Londres', 'LHR', 'london', 'LONDRES '):
            assert [h.nombre for h in hotel_recomendado_service.en(escrito)] == \
                ['The Hoxton'], escrito

    def test_ni_los_ocultos_ni_los_de_otra_ciudad(self, gestor, carlton):
        hotel_recomendado_service.crear(gestor, nombre='Oculto', ciudad='Lyon',
                                        activo=False)
        hotel_recomendado_service.crear(gestor, nombre='De París', ciudad='París')

        assert [h.nombre for h in hotel_recomendado_service.en('Lyon')] == \
            ['Hotel Carlton']

    def test_una_ciudad_sin_ninguno_lo_dice(self, gestor, carlton):
        opciones = travel_search_service.buscar_para(
            gestor, 'Barcelona', 'Oslo', date(2026, 12, 1), date(2026, 12, 3),
        )

        assert opciones['alojamiento'] == []
        assert any('no tiene hoteles recomendados' in a for a in opciones['avisos'])

    def test_ida_y_vuelta_en_el_dia_no_pide_hotel(self, gestor, carlton):
        opciones = travel_search_service.buscar_para(
            gestor, 'Barcelona', 'Lyon', date(2026, 12, 1), date(2026, 12, 1),
        )

        assert opciones['alojamiento'] == [] and not opciones['avisos']

    @pytest.mark.security
    def test_nunca_se_pregunta_por_hoteles_fuera(self, gestor, carlton, monkeypatch):
        """Not as a fallback either: where to stay is the organisation's call."""
        import httpx

        from tests.test_busqueda_de_viajes import _cliente_que_responde, _configurar

        registro = []
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(
            {'best_flights': [], 'other_flights': []}, registro=registro))

        travel_search_service.buscar_para(
            gestor, 'BCN', 'OSL', date(2026, 12, 1), date(2026, 12, 3))

        assert registro, 'los vuelos sí se buscan'
        assert all(p['params'].get('engine') != 'google_hotels' for p in registro)
        assert not hasattr(travel_search_service, 'buscar_alojamiento')

    def test_sale_en_la_pantalla_y_se_puede_marcar(self, as_user, gestor, carlton):
        with as_user(gestor) as client:
            html = client.post('/trips/planificar', data={
                'origen': 'Barcelona', 'destino': 'Lyon',
                'ida': '2026-12-01T00:00', 'vuelta': '2026-12-03T23:59', 'viajeros': 1,
            }).get_data(as_text=True)

        assert 'Hoteles recomendados' in html
        assert 'Hotel Carlton' in html
        assert 'name="alojamiento"' in html
        assert 'Tarifa corporativa CORP26.' in html

    def test_el_modelo_recibe_esa_lista(self, gestor, carlton, monkeypatch):
        """And nothing else about where to stay."""
        from app.services import ai_service

        vistos = []
        original = ai_service._run

        def espia(tarea, request, **kwargs):
            vistos.extend(b.contenido for b in request.bloques)
            return original(tarea, request, **kwargs)

        monkeypatch.setattr(ai_service, '_run', espia)
        opciones = travel_search_service.buscar_para(
            gestor, 'Barcelona', 'Lyon', date(2026, 12, 1), date(2026, 12, 3))
        ai_service.plan_trip(gestor, 'Barcelona', 'Lyon', opciones_reales=opciones)

        assert any('Hotel Carlton' in bloque for bloque in vistos)


class TestAlGuardarloEnElViaje:
    def test_lleva_la_tarifa_y_dice_de_donde_sale(self, gestor, carlton):
        from app.services import trip_from_search_service

        (fila,) = hotel_recomendado_service.como_opciones(
            [carlton], date(2026, 12, 1), date(2026, 12, 3))
        trip, _ = trip_from_search_service.crear_viaje(
            gestor, titulo='A Lyon', alojamientos=[fila], proyecto='PRJ-1',
            entrada=None, salida=None,
        )

        (hotel,) = [a for a in trip.accommodations if not a.is_deleted]
        assert hotel.nombre == 'Hotel Carlton'
        assert float(hotel.importe) == 240 and hotel.moneda == 'EUR'
        assert 'recomendado por la organización' in hotel.observaciones
        assert 'CORP26' in hotel.observaciones


def test_lyon_no_es_paris(gestor, seeded):
    """«Lyon» is contained in «Paris Gare de Lyon». Matching a city by a name
    that merely contains it filed a Lyon hotel under París."""
    hotel = hotel_recomendado_service.crear(gestor, nombre='Hotel Carlton', ciudad='Lyon')

    assert hotel.ciudad == 'Lyon'
    assert hotel_recomendado_service.en('París') == []
    assert hotel_recomendado_service.en('lyon') == [hotel]
