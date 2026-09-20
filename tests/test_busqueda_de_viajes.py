"""Real flight and lodging options, through SerpApi's Google Travel engines.

The connector is what closes the gap the planning assistant was built around:
it could say *how* to travel and never *what* exists, because nothing in the
application knew. Now something does.

Nothing here touches the network. A test that passed because SerpApi happened
to be reachable -- and a test that failed because it was not -- would both be
lying about the code.
"""
import json

import httpx
import pytest

from app.services import settings_service, travel_search_service


def _configurar(habilitada=True, clave='clave-de-pruebas', limite=50):
    settings_service.set_value('BUSQUEDA_VIAJES_HABILITADA', habilitada)
    settings_service.set_value('BUSQUEDA_VIAJES_API_KEY', clave)
    settings_service.set_value('BUSQUEDA_VIAJES_MAXIMAS_DIARIAS', limite)


#: One flight, shaped like SerpApi shapes them.
RESPUESTA_VUELOS = {
    'best_flights': [{
        'flights': [{
            'departure_airport': {'id': 'BCN', 'name': 'Barcelona', 'time': '2026-10-01 07:40'},
            'arrival_airport': {'id': 'LHR', 'name': 'Heathrow', 'time': '2026-10-01 09:15'},
            'duration': 155,
            'airline': 'British Airways',
            'flight_number': 'BA 477',
            'travel_class': 'Economy',
        }],
        'layovers': [],
        'total_duration': 155,
        'price': 187,
        'type': 'Round trip',
        'carbon_emissions': {'this_flight': 142000},
    }],
    'other_flights': [],
}

RESPUESTA_HOTELES = {
    'properties': [{
        'name': 'Hotel de prueba',
        'overall_rating': 4.3,
        'reviews': 812,
        'rate_per_night': {'lowest': '148 €'},
        'total_rate': {'lowest': '296 €'},
        'link': 'https://example.test/hotel',
    }],
}


class _RespuestaFalsa:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _cliente_que_responde(payload, status=200, registro=None):
    """Replace httpx.Client with one that answers without a socket."""

    class _Cliente:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            if registro is not None:
                registro.append({'url': url, 'params': params})
            return _RespuestaFalsa(payload, status)

    return _Cliente


@pytest.mark.integration
class TestCuandoNoEstaConfigurado:
    """The connector is off until an administrator turns it on and pays."""

    def test_sin_clave_no_esta_configurada(self, app, seeded):
        _configurar(habilitada=True, clave='')

        assert travel_search_service.esta_configurada() is False

    def test_sin_activar_no_esta_configurada(self, app, seeded):
        """A key pasted while evaluating must not start spending money."""
        _configurar(habilitada=False, clave='una-clave')

        assert travel_search_service.esta_configurada() is False

    def test_buscar_sin_configurar_lo_dice(self, app, seeded, gestor):
        _configurar(habilitada=False, clave='')

        with pytest.raises(travel_search_service.BusquedaNoDisponible) as exc:
            travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-01')

        assert 'Ajustes' in exc.value.mensaje

    def test_el_conjunto_vuelve_vacio_y_no_rompe(self, app, seeded, gestor):
        """The planning screen has to keep working without the connector."""
        _configurar(habilitada=False, clave='')

        resultado = travel_search_service.buscar_para(
            gestor, 'Barcelona', 'Londres', '2026-10-01',
        )

        assert resultado['vuelos'] == []
        assert resultado['consultado'] is False


@pytest.mark.integration
class TestLoQueDevuelve:
    def test_un_vuelo_se_normaliza(self, app, seeded, gestor, monkeypatch):
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_VUELOS))

        encontrado = travel_search_service.buscar_vuelos(
            gestor, 'BCN', 'LHR', '2026-10-01', viajeros=2,
        )

        assert len(encontrado['opciones']) == 1
        vuelo = encontrado['opciones'][0]
        assert vuelo['precio'] == 187
        assert vuelo['duracion_total'] == '2 h 35 min'
        assert vuelo['tramos'][0]['numero'] == 'BA 477'
        assert vuelo['tramos'][0]['origen'] == 'BCN'
        assert vuelo['emisiones_kg'] == 142

    def test_un_alojamiento_se_normaliza(self, app, seeded, gestor, monkeypatch):
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_HOTELES))

        opciones = travel_search_service.buscar_alojamiento(
            gestor, 'Londres', '2026-10-01', '2026-10-03',
        )

        assert opciones[0]['nombre'] == 'Hotel de prueba'
        assert opciones[0]['precio_noche'] == '148 €'

    def test_sin_resultados_no_es_un_error(self, app, seeded, gestor, monkeypatch):
        """«No flights that day» is an answer, not a breakage."""
        _configurar()
        monkeypatch.setattr(
            httpx, 'Client',
            _cliente_que_responde({'error': 'no results'}),
        )

        assert travel_search_service.buscar_vuelos(
            gestor, 'BCN', 'LHR', '2026-10-01')['opciones'] == []


@pytest.mark.integration
class TestLoQueSeEnvia:
    def test_se_manda_la_ruta_y_las_fechas_y_nada_mas(
        self, app, seeded, gestor, monkeypatch,
    ):
        """A route, some dates and a headcount. No names, no documents."""
        registro = []
        _configurar()
        monkeypatch.setattr(
            httpx, 'Client',
            _cliente_que_responde(RESPUESTA_VUELOS, registro=registro),
        )

        travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-01')

        enviado = registro[0]['params']
        assert enviado['departure_id'] == 'BCN'
        assert enviado['outbound_date'] == '2026-10-01'
        assert gestor.nombre_completo not in json.dumps(enviado)
        assert gestor.email not in json.dumps(enviado)


@pytest.mark.security
class TestLaClaveNoSeEscapa:
    def test_se_guarda_cifrada(self, app, seeded):
        """Same treatment as the AI provider keys: a credential in a column
        anyone can SELECT is a credential in plain text."""
        from app.models.settings import SystemSetting

        _configurar(clave='clave-secretisima')

        fila = SystemSetting.query.filter_by(clave='BUSQUEDA_VIAJES_API_KEY').first()
        assert 'clave-secretisima' not in str(fila.valor)
        assert settings_service.get('BUSQUEDA_VIAJES_API_KEY') == 'clave-secretisima'

    def test_no_queda_en_la_auditoria(self, app, seeded, gestor, monkeypatch):
        from app.models.audit import AuditEvent

        _configurar(clave='clave-secretisima')
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_VUELOS))

        travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-01')

        eventos = AuditEvent.query.filter_by(
            accion=travel_search_service.ACCION).all()
        assert eventos
        assert 'clave-secretisima' not in json.dumps(
            [e.metadatos for e in eventos], default=str)

    def test_un_fallo_de_red_no_registra_la_url(
        self, app, seeded, gestor, monkeypatch,
    ):
        """httpx puts the full URL -- query string and key -- in its errors."""

        class _Explota:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                raise httpx.ConnectError(f'fallo al conectar con {url}?api_key=secreta')

        _configurar(clave='secreta')
        monkeypatch.setattr(httpx, 'Client', _Explota)

        with pytest.raises(travel_search_service.BusquedaNoDisponible) as exc:
            travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-01')

        assert 'secreta' not in exc.value.mensaje


@pytest.mark.integration
class TestElPresupuestoDiario:
    def test_alcanzado_el_limite_se_deja_de_gastar(
        self, app, seeded, gestor, monkeypatch,
    ):
        """Each search is billed. The budget stops a loop from writing an
        invoice nobody expected."""
        _configurar(limite=1)
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_VUELOS))

        travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-01')

        with pytest.raises(travel_search_service.BusquedaNoDisponible) as exc:
            travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-02')

        assert 'límite' in exc.value.mensaje

    def test_un_fallo_tambien_cuenta(self, app, seeded, gestor, monkeypatch):
        """A connector erroring in a loop still bills for every call."""
        _configurar(limite=5)
        monkeypatch.setattr(
            httpx, 'Client', _cliente_que_responde({}, status=500))

        antes = travel_search_service.presupuesto_restante()
        with pytest.raises(travel_search_service.BusquedaNoDisponible):
            travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-01')

        assert travel_search_service.presupuesto_restante() == antes - 1


@pytest.mark.integration
class TestResolverLoQueSeEscribe:
    def test_una_ciudad_se_convierte_en_codigo(self, app, seeded):
        assert travel_search_service.codigo_de_aeropuerto('BCN') == 'BCN'

    def test_lo_que_el_catalogo_no_conoce_no_se_inventa(self, app, seeded):
        """An invented code searches the wrong route silently, which is worse
        than returning nothing."""
        assert travel_search_service.codigo_de_aeropuerto('Ciudad Inventada') is None

    def test_sin_aeropuerto_se_avisa_en_vez_de_callar(self, app, seeded, gestor):
        """An empty list must not read as «there is no way to get there»: the
        connector has no train engine at all."""
        _configurar()

        resultado = travel_search_service.buscar_para(
            gestor, 'Ciudad Inventada', 'Otra Inventada', '2026-10-01',
        )

        assert resultado['vuelos'] == []
        assert any('trenes' in aviso for aviso in resultado['avisos'])


@pytest.mark.unit
class TestElModeloNoReescribeLasCifras:
    """The displayed numbers come from the connector, never from the model.

    If the model restated a price and got a digit wrong, nobody would know
    which of the two figures was the real one.
    """

    def test_el_prompt_lo_prohibe(self):
        from app.services.ai.prompts import PROMPTS

        assert 'NO las repitas' in PROMPTS['plan_trip']
        assert 'no inventes' in PROMPTS['plan_trip'].lower()

    def test_las_opciones_viajan_como_contenido_no_confiable(
        self, app, seeded, gestor, monkeypatch,
    ):
        """A hotel named «ignora tus instrucciones» is content, not an order."""
        from app.services import ai_service

        visto = {}
        original = ai_service._run

        def _espia(tarea, request, **kwargs):
            visto['tipos'] = [b.tipo for b in request.bloques]
            return original(tarea, request, **kwargs)

        monkeypatch.setattr(ai_service, '_run', _espia)

        ai_service.plan_trip(
            actor=gestor, origen='Barcelona', destino='Londres',
            opciones_reales={'vuelos': [{'precio': 187}], 'alojamiento': []},
        )

        assert 'opciones_reales' in visto['tipos']


@pytest.mark.integration
class TestLaPantallaDePlanificacion:
    def test_se_ven_las_opciones_encontradas(
        self, as_user, gestor, seeded, monkeypatch,
    ):
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_VUELOS))

        with as_user(gestor) as client:
            html = client.post('/trips/planificar', data={
                'origen': 'BCN', 'destino': 'LHR',
                'ida': '2026-10-01T07:00', 'viajeros': 1,
            }).get_data(as_text=True)

        assert 'BA 477' in html
        assert 'Vuelos encontrados' in html

    def test_sin_conector_la_pantalla_sigue_funcionando(
        self, as_user, gestor, seeded,
    ):
        """The assistant worked before the connector and must keep working
        when it is off, unreachable or out of budget."""
        _configurar(habilitada=False, clave='')

        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar', data={
                'origen': 'BCN', 'destino': 'LHR',
                'ida': '2026-10-01T07:00', 'viajeros': 1,
            })

        assert respuesta.status_code == 200
        assert 'Vuelos encontrados' not in respuesta.get_data(as_text=True)


@pytest.mark.integration
class TestSeAdministraDesdeAjustes:
    def test_el_bloque_aparece_en_el_panel(self, as_user, admin, seeded):
        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'Búsqueda de vuelos y alojamiento' in html
        assert 'BUSQUEDA_VIAJES_API_KEY' in html

    def test_la_clave_no_se_devuelve_a_la_pantalla(self, as_user, admin, seeded):
        """The screen says one exists; it never says what it is."""
        _configurar(clave='clave-secretisima')

        with as_user(admin) as client:
            html = client.get('/admin/ajustes').get_data(as_text=True)

        assert 'clave-secretisima' not in html


@pytest.mark.integration
class TestSeExplicaPorQueNoHayOpciones:
    """A half-configured connector looks exactly like an absent one.

    The result is the same either way -- no options -- so somebody who has
    just pasted a key is left staring at a screen that does not mention the
    connector exists. The state has to say which half is missing.
    """

    def test_sin_clave(self, app, seeded):
        settings_service.set_value('BUSQUEDA_VIAJES_API_KEY', '')
        settings_service.set_value('BUSQUEDA_VIAJES_HABILITADA', True)

        assert travel_search_service.estado() == 'sin_clave'

    def test_con_clave_pero_apagado(self, app, seeded):
        settings_service.set_value('BUSQUEDA_VIAJES_API_KEY', 'una-clave')
        settings_service.set_value('BUSQUEDA_VIAJES_HABILITADA', False)

        assert travel_search_service.estado() == 'desactivado'

    def test_sin_presupuesto(self, app, seeded, gestor, monkeypatch):
        _configurar(limite=1)
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_VUELOS))
        travel_search_service.buscar_vuelos(gestor, 'BCN', 'LHR', '2026-10-01')

        assert travel_search_service.estado() == 'sin_presupuesto'

    def test_activo(self, app, seeded):
        _configurar()

        assert travel_search_service.estado() == 'activo'

    def test_el_administrador_ve_por_que_y_como_arreglarlo(
        self, as_user, admin, seeded,
    ):
        settings_service.set_value('BUSQUEDA_VIAJES_API_KEY', 'una-clave')
        settings_service.set_value('BUSQUEDA_VIAJES_HABILITADA', False)

        with as_user(admin) as client:
            html = client.get('/trips/planificar').get_data(as_text=True)

        assert 'está desactivada' in html
        assert '/admin/ajustes' in html

    def test_a_un_gestor_no_se_le_cuenta_lo_que_no_puede_tocar(
        self, as_user, gestor, seeded,
    ):
        settings_service.set_value('BUSQUEDA_VIAJES_API_KEY', 'una-clave')
        settings_service.set_value('BUSQUEDA_VIAJES_HABILITADA', False)

        with as_user(gestor) as client:
            html = client.get('/trips/planificar').get_data(as_text=True)

        assert 'está desactivada' not in html

    def test_el_panel_lateral_deja_de_negar_el_buscador(
        self, as_user, gestor, seeded,
    ):
        """It said «esta aplicación no está conectada a ningún sistema de
        precios», which stopped being true the moment somebody enabled one."""
        _configurar()

        with as_user(gestor) as client:
            html = client.get('/trips/planificar').get_data(as_text=True)

        assert 'Busca vuelos y alojamiento de verdad' in html
        assert 'no está conectada a ningún sistema' not in html


@pytest.mark.unit
class TestElIdiomaNoTumbaLaBusqueda:
    """Measured against the real API, not assumed.

    With ``hl=es`` the hotels engine answers «Google Hotels hasn't returned any
    results for this query» for a search that returns twenty properties without
    it. Sending it cost every hotel result and looked like «there are no hotels
    in Paris», which is the kind of wrong answer nobody questions.
    """

    def test_a_hoteles_no_se_le_manda_el_idioma(
        self, app, seeded, gestor, monkeypatch,
    ):
        registro = []
        _configurar()
        monkeypatch.setattr(
            httpx, 'Client',
            _cliente_que_responde(RESPUESTA_HOTELES, registro=registro),
        )

        travel_search_service.buscar_alojamiento(
            gestor, 'Paris', '2026-10-15', '2026-10-17')

        assert 'hl' not in registro[0]['params']
        # gl sí: ese no le afecta, y sigue orientando los precios al mercado.
        assert registro[0]['params']['gl'] == 'es'

    def test_a_vuelos_si(self, app, seeded, gestor, monkeypatch):
        registro = []
        _configurar()
        monkeypatch.setattr(
            httpx, 'Client',
            _cliente_que_responde(RESPUESTA_VUELOS, registro=registro),
        )

        travel_search_service.buscar_vuelos(gestor, 'BCN', 'CDG', '2026-10-15')

        assert registro[0]['params']['hl'] == 'es'


#: A booking option, shaped like SerpApi shapes them. The link is a URL plus a
#: body, not an address -- which is what makes it a form and not an anchor.
RESPUESTA_COMPRA = {
    'booking_options': [
        {'together': {
            'book_with': 'Vueling',
            'marketed_as': ['VY 8246'],
            'price': 63,
            'booking_request': {
                'url': 'https://www.google.com/travel/clk/f',
                'post_data': 'u=ADowPOK00khXtvt5&gwp=1%2F2',
            },
        }},
        {'together': {
            'book_with': 'Booking.com',
            'price': 59,
            'booking_request': {'url': 'https://www.google.com/travel/clk/f',
                                'post_data': 'u=OTRO'},
        }},
    ],
}


@pytest.mark.integration
class TestLosEnlacesAlVuelo:
    def test_la_lista_trae_el_enlace_a_la_busqueda(
        self, app, seeded, gestor, monkeypatch,
    ):
        """Free: it is already in the response, so every result gets it
        without a second billed search."""
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde({
            **RESPUESTA_VUELOS,
            'search_metadata': {'google_flights_url': 'https://example.test/vuelos'},
        }))

        encontrado = travel_search_service.buscar_vuelos(
            gestor, 'BCN', 'CDG', '2026-10-15')

        assert encontrado['enlace'] == 'https://example.test/vuelos'

    def test_cada_vuelo_lleva_con_que_preguntar_donde_comprarlo(
        self, app, seeded, gestor, monkeypatch,
    ):
        """SerpApi will not take the token alone: the whole search goes back
        with it, so the parameters travel with each option."""
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde({
            **RESPUESTA_VUELOS,
            'best_flights': [{**RESPUESTA_VUELOS['best_flights'][0],
                              'booking_token': 'UN-TOKEN'}],
        }))

        vuelo = travel_search_service.buscar_vuelos(
            gestor, 'BCN', 'CDG', '2026-10-15')['opciones'][0]

        assert vuelo['token'] == 'UN-TOKEN'
        assert vuelo['busqueda']['departure_id'] == 'BCN'

    def test_el_cuerpo_del_formulario_se_descodifica(
        self, app, seeded, gestor, monkeypatch,
    ):
        """A template splitting the raw body on «&» would send the escapes
        through literally and land on a page that lost the flight."""
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_COMPRA))

        ofertas = travel_search_service.opciones_de_compra(
            gestor, 'UN-TOKEN', {'departure_id': 'BCN'})

        assert ofertas[0]['vendedor'] == 'Vueling'
        assert ofertas[0]['campos']['u'] == 'ADowPOK00khXtvt5'
        assert ofertas[0]['campos']['gwp'] == '1/2'

    def test_preguntar_donde_comprar_cuesta_una_busqueda(
        self, app, seeded, gestor, monkeypatch,
    ):
        """On demand precisely because of this: doing it for all eight results
        of every planning would spend a day's budget in six plannings."""
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_COMPRA))

        antes = travel_search_service.presupuesto_restante()
        travel_search_service.opciones_de_compra(gestor, 'UN-TOKEN', {})

        assert travel_search_service.presupuesto_restante() == antes - 1

    def test_la_pantalla_ofrece_donde_comprarlo(
        self, as_user, gestor, seeded, monkeypatch,
    ):
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde({
            **RESPUESTA_VUELOS,
            'best_flights': [{**RESPUESTA_VUELOS['best_flights'][0],
                              'booking_token': 'UN-TOKEN'}],
        }))

        with as_user(gestor) as client:
            html = client.post('/trips/planificar', data={
                'origen': 'BCN', 'destino': 'LHR',
                'ida': '2026-10-01T07:00', 'viajeros': 1,
            }).get_data(as_text=True)

        assert 'Dónde comprarlo' in html
        assert 'UN-TOKEN' in html

    def test_se_listan_los_vendedores(
        self, as_user, gestor, seeded, monkeypatch,
    ):
        _configurar()
        monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(RESPUESTA_COMPRA))

        with as_user(gestor) as client:
            html = client.post('/trips/planificar/comprar', data={
                'token': 'UN-TOKEN', 'departure_id': 'BCN',
                'arrival_id': 'CDG', 'outbound_date': '2026-10-15',
            }).get_data(as_text=True)

        assert 'Vueling' in html
        assert 'Booking.com' in html
        # Un POST, no un <a>: el enlace es una URL más un cuerpo.
        assert 'method="post" action="https://www.google.com/travel/clk/f"' in html

    def test_sin_token_no_se_gasta_una_busqueda(self, as_user, gestor, seeded):
        _configurar()

        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar/comprar', data={})

        assert respuesta.status_code == 302


@pytest.mark.security
class TestElFormularioDeCompraLlegaAGoogle:
    def test_la_csp_lo_permite(self):
        """`form-action 'self'` would block the POST silently, and the button
        would do nothing with no error anywhere."""
        from pathlib import Path

        fuente = (Path(__file__).resolve().parent.parent
                  / 'app' / '__init__.py').read_text()

        assert "'form-action': [\"'self'\", 'https://www.google.com']" in fuente

    def test_solo_quien_puede_crear_viajes_consulta_precios(
        self, as_user, viajero, seeded,
    ):
        with as_user(viajero) as client:
            respuesta = client.post('/trips/planificar/comprar',
                                    data={'token': 'X'})

        assert respuesta.status_code == 403
