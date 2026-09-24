"""How the connector's flights reach the screen.

Eight options for the same day are chosen by comparing three numbers -- the
time, the duration and the price -- so the work here is to make those
comparable: the date split off the time, the airlines collapsed, the extra day
marked, and the price carried as a number so the page can rank without parsing
back the string it just printed.

The return half is separate on purpose. On a round trip the engine answers with
outbound journeys only and prices the *whole* trip, which is a figure that
means two different things if nobody says which.
"""
import pytest

from app.services import travel_search_service as buscador

pytestmark = pytest.mark.unit


IDA_Y_VUELTA = {
    'flights': [
        {
            'departure_airport': {'id': 'BCN', 'name': 'Barcelona',
                                  'time': '2026-09-28 22:40'},
            'arrival_airport': {'id': 'ZRH', 'name': 'Zúrich',
                                'time': '2026-09-29 00:30'},
            'duration': 110, 'airline': 'SWISS', 'flight_number': 'LX 1953',
        },
        {
            'departure_airport': {'id': 'ZRH', 'name': 'Zúrich',
                                  'time': '2026-09-29 06:30'},
            'arrival_airport': {'id': 'CDG', 'name': 'París',
                                'time': '2026-09-29 07:50'},
            'duration': 80, 'airline': 'SWISS', 'flight_number': 'LX 644',
        },
    ],
    'layovers': [{'name': 'Zúrich', 'id': 'ZRH', 'duration': 360}],
    'total_duration': 550,
    'price': 205,
    'carbon_emissions': {'this_flight': 151000},
    'departure_token': 'UN-TOKEN-DE-IDA',
}


class TestLaHoraSeSeparaDeLaFecha:
    def test_el_tramo_trae_hora_y_fecha_aparte(self, app, seeded):
        opcion = buscador._opcion_de_vuelo(IDA_Y_VUELTA)

        assert opcion['hora_salida'] == '22:40'
        assert opcion['hora_llegada'] == '07:50'
        assert opcion['fecha_salida'].isoformat() == '2026-09-28'

    def test_una_hora_ilegible_no_pierde_el_dato(self):
        """An odd timestamp still describes a real option."""
        fecha, hora = buscador._instante('mañana por la tarde')

        assert fecha is None
        assert hora == 'mañana por la tarde'

    def test_sin_hora_no_inventa_ninguna(self):
        assert buscador._instante(None) == (None, None)


class TestLoQueDistingueUnaOpcionDeOtra:
    def test_se_marca_que_llega_al_dia_siguiente(self, app, seeded):
        """Otherwise a flight landing at 07:50 looks like the fastest one."""
        opcion = buscador._opcion_de_vuelo(IDA_Y_VUELTA)

        assert opcion['dias_extra'] == 1

    def test_un_vuelo_del_mismo_dia_no_lleva_marca(self, app, seeded):
        directo = {
            'flights': [{
                'departure_airport': {'id': 'BCN', 'time': '2026-09-28 10:50'},
                'arrival_airport': {'id': 'CDG', 'time': '2026-09-28 12:50'},
                'duration': 120, 'airline': 'Vueling', 'flight_number': 'VY 8250',
            }],
            'layovers': [], 'total_duration': 120, 'price': 222,
        }

        assert buscador._opcion_de_vuelo(directo)['dias_extra'] == 0

    def test_una_conexion_del_mismo_operador_se_nombra_una_vez(self, app, seeded):
        opcion = buscador._opcion_de_vuelo(IDA_Y_VUELTA)

        assert opcion['aerolineas'] == ['SWISS']

    def test_el_precio_viaja_tambien_como_numero(self, app, seeded):
        """So the page can rank without parsing back the string it printed."""
        opcion = buscador._opcion_de_vuelo(IDA_Y_VUELTA)

        assert opcion['precio'] == '205 €'
        assert opcion['precio_valor'] == 205

    def test_un_precio_ya_formateado_no_se_ordena_a_ciegas(self, app, seeded):
        """The hotels engine hands over «€66»; guessing a number from it is
        how two prices end up compared as strings."""
        opcion = buscador._opcion_de_vuelo({**IDA_Y_VUELTA, 'price': '205 €'})

        assert opcion['precio_valor'] is None


class TestLaVueltaEsOtraBusqueda:
    def test_la_ida_trae_el_token_con_el_que_pedirla(self, app, seeded, gestor,
                                                     monkeypatch):
        _preparar(monkeypatch, {'best_flights': [IDA_Y_VUELTA], 'other_flights': []})

        encontrado = buscador.buscar_vuelos(
            gestor, 'BCN', 'CDG', '2026-09-28', vuelta='2026-10-02',
        )

        assert encontrado['opciones'][0]['token_vuelta'] == 'UN-TOKEN-DE-IDA'

    def test_sin_fecha_de_vuelta_no_hay_token(self, app, seeded, gestor, monkeypatch):
        sin_token = {k: v for k, v in IDA_Y_VUELTA.items() if k != 'departure_token'}
        _preparar(monkeypatch, {'best_flights': [sin_token], 'other_flights': []})

        encontrado = buscador.buscar_vuelos(gestor, 'BCN', 'CDG', '2026-09-28')

        assert encontrado['opciones'][0]['token_vuelta'] is None

    def test_pedir_las_vueltas_manda_el_token(self, app, seeded, gestor, monkeypatch):
        registro = []
        _preparar(monkeypatch, {'best_flights': [IDA_Y_VUELTA], 'other_flights': []},
                  registro)

        buscador.vuelos_de_vuelta(
            gestor, 'UN-TOKEN-DE-IDA',
            {'departure_id': 'BCN', 'arrival_id': 'CDG',
             'outbound_date': '2026-09-28', 'return_date': '2026-10-02', 'type': '1'},
        )

        assert registro[-1]['params']['departure_token'] == 'UN-TOKEN-DE-IDA'
        # La búsqueda original viaja entera: el motor no acepta el token solo.
        assert registro[-1]['params']['outbound_date'] == '2026-09-28'

    def test_la_consulta_de_vueltas_tambien_se_factura(self, app, seeded, gestor,
                                                       monkeypatch):
        """It is a second billed search, so the daily budget has to see it."""
        from app.models.audit import AuditEvent

        _preparar(monkeypatch, {'best_flights': [IDA_Y_VUELTA], 'other_flights': []})
        antes = AuditEvent.query.filter_by(accion=buscador.ACCION).count()

        buscador.vuelos_de_vuelta(gestor, 'T', {'departure_id': 'BCN'})

        assert AuditEvent.query.filter_by(accion=buscador.ACCION).count() == antes + 1


class TestLaPantallaDiceQueSignificaElPrecio:
    def test_con_vuelta_el_conjunto_lo_anuncia(self, app, seeded, gestor, monkeypatch):
        """«222 €» means the whole trip here and one leg elsewhere."""
        _preparar(monkeypatch, {'best_flights': [IDA_Y_VUELTA], 'other_flights': []})

        resultado = buscador.buscar_para(
            gestor, 'Barcelona', 'París', '2026-09-28', vuelta='2026-10-02',
        )

        assert resultado['hay_vuelta'] is True

    def test_sin_vuelta_no(self, app, seeded, gestor, monkeypatch):
        _preparar(monkeypatch, {'best_flights': [IDA_Y_VUELTA], 'other_flights': []})

        resultado = buscador.buscar_para(gestor, 'Barcelona', 'París', '2026-09-28')

        assert resultado['hay_vuelta'] is False


def _preparar(monkeypatch, payload, registro=None):
    """Connector on, with an HTTP client that answers without a socket."""
    import httpx

    from tests.test_busqueda_de_viajes import _cliente_que_responde, _configurar

    # El presupuesto diario se cuenta de la auditoría, que en la suite se
    # acumula sobre la misma base: con el límite por defecto, estos tests
    # pasan solos y fallan al final de una ejecución completa, que es la
    # forma más cara posible de descubrir que no era culpa del código.
    _configurar(habilitada=True, clave='una-clave', limite=100_000)
    monkeypatch.setattr(httpx, 'Client', _cliente_que_responde(payload, 200, registro))


DIRECTO = {
    'flights': [{
        'departure_airport': {'id': 'BCN', 'time': '2026-09-28 10:50'},
        'arrival_airport': {'id': 'CDG', 'time': '2026-09-28 12:50'},
        'duration': 120, 'airline': 'Vueling', 'flight_number': 'VY 8250',
    }],
    'layovers': [], 'total_duration': 120, 'price': 222,
    'carbon_emissions': {'this_flight': 97000},
}

INSIGHTS = {
    'lowest_price': 205,
    'price_level': 'low',
    'typical_price_range': [190, 350],
    'price_history': [[1758000000, 246], [1758600000, 205], [1759200000, 289]],
}

PLANTILLA = (
    '{% from "macros/vuelos.html" import lista_de_vuelos %}'
    '{{ lista_de_vuelos(vuelos, "Ida", fecha, "https://x", none,'
    ' precio_es_total=precio_es_total, accion_vueltas="/vueltas", csrf="c",'
    ' precios=precios) }}'
)


def _pintar(app, opciones, precios=None, precio_es_total=True):
    from datetime import date

    from flask import render_template_string

    vuelos = [buscador._opcion_de_vuelo(o) for o in opciones]
    for vuelo in vuelos:
        vuelo['busqueda'] = {'departure_id': 'BCN'}
        vuelo['token'] = 'X'
        vuelo['token_vuelta'] = 'T'

    with app.test_request_context('/'):
        return render_template_string(
            PLANTILLA, vuelos=vuelos, fecha=date(2026, 9, 28),
            precios=precios, precio_es_total=precio_es_total,
        )


class TestLaListaSeLeeComparando:
    def test_la_fecha_sale_una_vez_en_la_cabecera(self, app, seeded):
        """Not four times per row: it is the same day for every option, and
        repeating it buries the one thing that tells them apart.

        Counted on the visible text and not on the markup: the row travels as
        JSON inside a hidden field so the return search can re-post it, and
        that JSON carries the raw timestamps. Nobody reads those.
        """
        import re

        html = _pintar(app, [DIRECTO, IDA_Y_VUELTA])
        visible = re.sub(r'<[^>]+>', ' ', html)

        assert 'lunes, 28 sep 2026' in html
        assert visible.count('2026-09-28') == 0

    def test_la_hora_se_muestra_sola(self, app, seeded):
        html = _pintar(app, [DIRECTO])

        assert '>10:50<' in html
        assert '>12:50<' in html

    def test_el_que_llega_al_dia_siguiente_lo_avisa(self, app, seeded):
        html = _pintar(app, [IDA_Y_VUELTA])

        assert 'vuelo-dia-siguiente' in html

    def test_con_vuelta_se_dice_que_el_precio_es_del_viaje_entero(self, app, seeded):
        html = _pintar(app, [DIRECTO], precio_es_total=True)

        assert 'ida y vuelta' in html

    def test_sin_vuelta_no_se_dice(self, app, seeded):
        html = _pintar(app, [DIRECTO], precio_es_total=False)

        assert 'ida y vuelta' not in html

    def test_los_tramos_de_una_escala_van_replegados(self, app, seeded):
        """Two rows of leg detail per option is what made eight options
        unreadable; it is still there, one click away."""
        html = _pintar(app, [IDA_Y_VUELTA])

        assert '<details' in html
        assert 'LX 1953' in html

    def test_un_vuelo_directo_no_trae_desplegable(self, app, seeded):
        html = _pintar(app, [DIRECTO])

        assert '<details' not in html


class TestElMasBaratoSeVeSinBuscarlo:
    def test_la_fila_entera_va_marcada(self, app, seeded):
        html = _pintar(app, [DIRECTO, IDA_Y_VUELTA])

        assert html.count('vuelo-barato') == 1

    def test_la_marca_cae_en_el_precio_menor(self, app, seeded):
        """205 beats 222, whatever order the engine sent them in."""
        html = _pintar(app, [DIRECTO, IDA_Y_VUELTA])
        fila = html[html.index('vuelo-barato'):]

        assert '205' in fila[:fila.index('</li>')]

    def test_sin_precios_comparables_no_se_marca_ninguno(self, app, seeded):
        """A price the engine sent pre-formatted cannot be ranked, and
        guessing a winner is worse than marking none."""
        html = _pintar(app, [{**DIRECTO, 'price': '222 €'}])

        assert 'vuelo-barato' not in html


class TestElGraficoDePrecios:
    def test_se_dibuja_con_lo_que_vino_en_la_misma_respuesta(self, app, seeded):
        html = _pintar(app, [DIRECTO], precios=buscador._analisis_de_precios(INSIGHTS))

        assert 'vuelo-precios-grafico' in html
        assert 'Habitual entre' in html

    def test_el_minimo_del_historico_va_en_el_mismo_verde(self, app, seeded):
        """Two places saying «this is the cheap one» have to say it alike."""
        html = _pintar(app, [DIRECTO], precios=buscador._analisis_de_precios(INSIGHTS))

        assert 'es-minimo' in html

    def test_sin_historico_no_se_dibuja_un_marco_vacio(self, app, seeded):
        """An empty frame reads as «there is no price history», and what it
        means is that nobody asked."""
        html = _pintar(app, [DIRECTO], precios=None)

        assert 'vuelo-precios-grafico' not in html

    def test_una_respuesta_sin_analisis_no_inventa_uno(self, app, seeded):
        assert buscador._analisis_de_precios(None) is None
        assert buscador._analisis_de_precios({}) is None

    def test_cada_punto_lleva_su_precio_para_el_raton(self, app, seeded):
        """Reading a series by pointing at it is reading figures, so the figure
        has to be there -- and with its currency, like every other price."""
        html = _pintar(app, [DIRECTO], precios=buscador._analisis_de_precios(INSIGHTS))

        assert 'vuelo-punto' in html
        assert '246 €' in html
        assert '289 €' in html

    def test_el_precio_del_punto_se_formatea_en_el_servicio(self, app, seeded):
        analisis = buscador._analisis_de_precios(INSIGHTS)

        assert analisis['historico'][0]['texto'] == '246 €'

    def test_no_se_deja_el_globo_del_navegador(self, app, seeded):
        """Both would show, one after the other and each saying the same."""
        html = _pintar(app, [DIRECTO], precios=buscador._analisis_de_precios(INSIGHTS))
        grafico = html[html.index('vuelo-precios-grafico'):html.index('list-group')]

        assert 'title=' not in grafico

    def test_un_punto_ilegible_no_tumba_el_grafico(self, app, seeded):
        analisis = buscador._analisis_de_precios({
            'price_level': 'typical',
            'price_history': [[1758000000, 246], ['x', 'y'], [1759200000, 289]],
        })

        assert len(analisis['historico']) == 2


@pytest.mark.integration
class TestLaPantallaDeVueltas:
    def test_pide_las_vueltas_y_las_pinta(self, as_user, gestor, seeded, monkeypatch):
        _preparar(monkeypatch, {'best_flights': [DIRECTO], 'other_flights': []})

        with as_user(gestor) as client:
            html = client.post('/trips/planificar/vueltas', data={
                'token': 'UN-TOKEN-DE-IDA', 'departure_id': 'CDG',
                'arrival_id': 'BCN', 'outbound_date': '2026-09-28',
                'return_date': '2026-10-02', 'type': '1',
            }).get_data(as_text=True)

        assert 'Vuelta' in html
        assert 'VY 8250' in html
        # El precio es el del viaje entero, y la pantalla tiene que decirlo.
        assert 'ida y vuelta' in html

    def test_sin_token_no_se_gasta_una_busqueda(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar/vueltas', data={})

        assert respuesta.status_code == 302

    def test_solo_quien_puede_crear_viajes_las_consulta(self, as_user, viajero, seeded):
        with as_user(viajero) as client:
            respuesta = client.post('/trips/planificar/vueltas', data={'token': 'X'})

        assert respuesta.status_code == 403


@pytest.mark.unit
class TestQueAeropuertosSeBuscan:
    """Un viaje desde París volvía con alojamiento y sin un solo vuelo.

    El catálogo tiene «PAR» para poder nombrar la ciudad cuando una reserva
    dice «a París» y no a una terminal, pero el motor de vuelos no tiene nada
    saliendo de «PAR»: devolvía cero y la pantalla lo enseñaba como si no
    hubiera vuelos. Y al mirarlo apareció lo de al lado: una ciudad con varios
    aeropuertos se resolvía a uno solo, así que buscar «Londres» enseñaba los
    vuelos de Gatwick y ninguno de Heathrow, sin decir por qué.
    """

    def test_un_codigo_de_ciudad_se_expande_a_sus_aeropuertos(self, app, seeded):
        assert buscador.codigo_de_aeropuerto('PAR') == 'CDG,ORY'

    def test_una_ciudad_con_varios_los_busca_todos(self, app, seeded):
        assert buscador.codigo_de_aeropuerto('Londres') == 'LGW,LHR,STN'

    def test_tambien_escrita_en_otro_idioma(self, app, seeded):
        assert buscador.codigo_de_aeropuerto('London') == 'LGW,LHR,STN'

    def test_un_aeropuerto_escrito_a_pelo_manda(self, app, seeded):
        """Quien pide CDG no quiere además Orly."""
        assert buscador.codigo_de_aeropuerto('CDG') == 'CDG'

    def test_una_ciudad_con_uno_solo_sigue_igual(self, app, seeded):
        assert buscador.codigo_de_aeropuerto('Madrid') == 'MAD'

    def test_lo_desconocido_no_se_inventa(self, app, seeded):
        """Vuelos de la ciudad equivocada son peores que ninguno."""
        assert buscador.codigo_de_aeropuerto('Ciudad Inexistente') is None

    def test_los_codigos_metropolitanos_no_son_aeropuertos(self, app, seeded):
        """Mientras lo sean, el buscador les pedirá vuelos y no habrá."""
        from app.models.catalog import Location
        from app.models.enums import LocationKind

        metropolitanos = Location.query.filter(
            Location.codigo.in_(['PAR', 'LON', 'NYC', 'MIL', 'ROM'])
        ).all()

        assert metropolitanos
        for fila in metropolitanos:
            assert fila.tipo == LocationKind.CIUDAD, fila.codigo

    def test_siguen_sirviendo_para_nombrar_la_ciudad(self, app, seeded):
        """Que no sean aeropuertos no puede romper el itinerario: un tramo con
        destino «LON» tiene que seguir situándose en Londres."""
        from app.services import map_service

        assert map_service.ciudad_del_catalogo(codigo='LON') == ('Londres', 'GB')


@pytest.mark.unit
class TestSembrarRefrescaLoQueYaEstaba:
    def test_se_guarda_aunque_no_se_cree_ninguna_fila(self, app, seeded):
        """«seed_catalogs» solo confirmaba si había creado algo, así que los
        refrescos de filas existentes se descartaban en silencio en cualquier
        instalación que ya tuviera el catálogo."""
        from app.extensions import db
        from app.models.catalog import Location
        from app.models.enums import LocationKind
        from app.services import seed_service

        fila = Location.query.filter_by(codigo='PAR').first()
        fila.tipo = LocationKind.AEROPUERTO
        fila.alias = None
        db.session.commit()

        creadas = seed_service.seed_catalogs()[1]
        db.session.expire_all()

        assert creadas == 0, 'este test no vale si además crea filas'
        assert Location.query.filter_by(codigo='PAR').first().tipo \
            == LocationKind.CIUDAD
