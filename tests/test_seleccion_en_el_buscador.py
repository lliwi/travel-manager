"""La selección del buscador, recorrida por donde la recorre un navegador.

El fallo que esto existe para que no vuelva: las filas del buscador viajan como
JSON dentro del atributo «value» de una casilla, y se escribían con «tojson»,
que escapa «<», «>», «&» y la comilla simple -- está pensado para meter datos
dentro de un «<script>» -- pero **no la comilla doble**. El navegador leía
«value="{"» y convertía el resto del JSON en atributos sueltos, así que al
enviar el formulario no llegaba nada y la pantalla respondía «marque al menos
un vuelo o un alojamiento» con todo marcado.

Las pruebas de la creación no lo veían porque enviaban el JSON directamente,
saltándose el HTML. Estas parsean la página como lo haría un navegador.
"""
import json
from html.parser import HTMLParser

import pytest

pytestmark = pytest.mark.integration


class _Casillas(HTMLParser):
    """Lo que un navegador enviaría por cada casilla marcada."""

    def __init__(self, nombre):
        super().__init__()
        self.nombre = nombre
        self.valores = []
        self.formularios = []

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)
        if tag == 'form' and atributos.get('id'):
            self.formularios.append(atributos['id'])
        if tag == 'input' and atributos.get('name') == self.nombre:
            self.valores.append(atributos.get('value'))


def _casillas(html, nombre):
    lector = _Casillas(nombre)
    lector.feed(html)
    return lector


@pytest.fixture
def resultados(as_user, gestor, seeded, monkeypatch):
    """La pantalla de planificación con opciones reales dibujadas."""
    from tests.test_vuelos_legibles import DIRECTO, IDA_Y_VUELTA, _preparar

    _preparar(monkeypatch, {
        'best_flights': [DIRECTO, IDA_Y_VUELTA], 'other_flights': [],
        'properties': [{
            'name': 'Zedwell Piccadilly Circus', 'overall_rating': 4.3,
            'rate_per_night': {'lowest': '148 €'},
            'total_rate': {'lowest': '327 €'},
            'link': 'https://hotel.test/reservar',
        }],
    })

    with as_user(gestor) as client:
        yield client.post('/trips/planificar', data={
            'origen': 'BCN', 'destino': 'LON',
            'ida': '2026-09-28T09:00', 'vuelta': '2026-10-02T18:00',
            'viajeros': 1,
        }).get_data(as_text=True)


class TestLoQueElNavegadorEnviaria:
    def test_el_valor_de_la_casilla_es_json_entero(self, resultados):
        """Con «tojson» a secas el navegador leía «{» y tiraba el resto."""
        leido = _casillas(resultados, 'vuelo')

        assert leido.valores, 'no hay casillas de vuelo en la página'
        for valor in leido.valores:
            fila = json.loads(valor)
            assert fila.get('tramos'), f'JSON incompleto: {valor[:60]}'

    def test_y_el_del_alojamiento_tambien(self, resultados):
        leido = _casillas(resultados, 'alojamiento')

        assert leido.valores
        for valor in leido.valores:
            assert json.loads(valor).get('nombre')

    def test_las_casillas_apuntan_a_un_formulario_que_existe(self, resultados):
        """«form=» a un id inexistente no envía nada y no avisa de nada."""
        lector = _Casillas('vuelo')
        lector.feed(resultados)

        assert 'seleccion' in lector.formularios


class TestElCaminoCompleto:
    def test_marcar_y_enviar_crea_el_viaje(self, as_user, gestor, resultados):
        """Lo que hace un navegador: coger los «value» de la página y
        reenviarlos. Es el paso que faltaba por cubrir."""
        from app.models.trip import Trip

        vuelos = _casillas(resultados, 'vuelo').valores
        alojamientos = _casillas(resultados, 'alojamiento').valores

        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar/crear', data={
                'csrf_token': 'x', 'titulo': 'BCN → LON',
                'vuelo': vuelos[:1], 'alojamiento': alojamientos[:1],
                'entrada': '2026-09-28', 'salida': '2026-10-02',
            }, follow_redirects=True)

        html = respuesta.get_data(as_text=True)

        assert 'Marque al menos' not in html
        assert Trip.query.count() >= 1

    def test_sin_marcar_nada_si_lo_dice(self, as_user, gestor, seeded):
        with as_user(gestor) as client:
            html = client.post('/trips/planificar/crear', data={'csrf_token': 'x'},
                               follow_redirects=True).get_data(as_text=True)

        assert 'Marque al menos' in html


class TestLasFechasDeLaBusqueda:
    def test_sin_ida_no_se_busca(self, as_user, gestor, seeded):
        """Sin ida el conector no busca nada y el asistente solo puede hablar
        de la ruta en abstracto."""
        with as_user(gestor) as client:
            html = client.post('/trips/planificar', data={
                'origen': 'BCN', 'destino': 'LON', 'viajeros': 1,
            }).get_data(as_text=True)

        assert 'Indique la fecha de ida' in html

    def test_la_vuelta_si_es_opcional(self, as_user, gestor, seeded):
        """Hay viajes de solo ida, y exigirla obligaría a inventar una fecha."""
        with as_user(gestor) as client:
            html = client.post('/trips/planificar', data={
                'origen': 'BCN', 'destino': 'LON',
                'ida': '2026-09-28T09:00', 'viajeros': 1,
            }).get_data(as_text=True)

        assert 'Indique la fecha de vuelta' not in html
        assert 'Planificar un viaje' in html

    def test_la_vuelta_no_puede_ir_antes_de_la_ida(self, as_user, gestor, seeded):
        """La búsqueda lo aceptaría y devolvería una lista vacía, que se lee
        como «no hay vuelos»."""
        with as_user(gestor) as client:
            html = client.post('/trips/planificar', data={
                'origen': 'BCN', 'destino': 'LON',
                'ida': '2026-10-02T09:00', 'vuelta': '2026-09-28T18:00',
                'viajeros': 1,
            }).get_data(as_text=True)

        assert 'anterior a la ida' in html


class TestElAlojamientoLlegaALaPantallaDeVueltas:
    """Al pulsar «Ver vueltas» se perdía lo que hubieras marcado de
    alojamiento, y allí no había forma de volver a marcarlo sin repetir la
    búsqueda -- otra consulta facturada que además podría devolver otros
    precios que los que ya se vieron.
    """

    def _ver_vueltas(self, client, resultados):
        """Reenvía el formulario de «Ver vueltas» tal y como lo haría el
        navegador: con la ida y con los alojamientos que lleva dentro."""
        idas = _casillas(resultados, 'ida').valores
        hoteles = _casillas(resultados, 'alojamiento').valores

        assert idas, 'la página no lleva la ida en el formulario de vueltas'

        return client.post('/trips/planificar/vueltas', data={
            'token': 'UN-TOKEN', 'departure_id': 'BCN', 'arrival_id': 'LON',
            'outbound_date': '2026-09-28', 'return_date': '2026-10-02',
            'type': '1', 'ida': idas[0], 'alojamiento': hoteles,
        }).get_data(as_text=True)

    def test_la_pagina_de_ida_lo_lleva_en_el_formulario(self, resultados):
        leido = _casillas(resultados, 'alojamiento')

        assert leido.valores, 'el alojamiento no viaja con «Ver vueltas»'
        assert json.loads(leido.valores[0]).get('nombre')

    def test_y_se_puede_marcar_alli(self, as_user, gestor, resultados, monkeypatch):
        from tests.test_vuelos_legibles import DIRECTO, _preparar

        _preparar(monkeypatch, {'best_flights': [DIRECTO], 'other_flights': []})

        with as_user(gestor) as client:
            html = self._ver_vueltas(client, resultados)

        assert 'Alojamiento encontrado' in html
        assert 'Zedwell' in html

    def test_una_fila_ilegible_no_tumba_la_pantalla(self, as_user, gestor,
                                                    seeded, monkeypatch):
        """La página tiene que dibujarse sin esa fila, no fallar entera."""
        import json as _json

        from tests.test_vuelos_legibles import DIRECTO, IDA_Y_VUELTA, _preparar

        _preparar(monkeypatch, {'best_flights': [DIRECTO], 'other_flights': []})

        with as_user(gestor) as client:
            respuesta = client.post('/trips/planificar/vueltas', data={
                'token': 'T', 'departure_id': 'BCN', 'arrival_id': 'LON',
                'ida': _json.dumps({'tramos': IDA_Y_VUELTA['flights']}),
                'alojamiento': ['esto no es json'],
            })

        assert respuesta.status_code == 200


class TestElAvisoSeRetiraAlRellenar:
    """El aviso de un campo obligatorio lo dibuja el servidor y se queda
    quieto: se envía en blanco, salen los mensajes, se rellenan los campos y
    los mensajes siguen ahí hasta el siguiente envío. Leído desde fuera, eso
    dice que lo que acabas de escribir tampoco vale.

    Lo retira «main.js» al escribir. Aquí se fija el contrato de marcado del
    que depende: sin «is-invalid» en el campo y sin un «.invalid-feedback»
    hermano, el script deja de encontrar qué ocultar y nadie se entera.
    """

    def _formulario_en_blanco(self, as_user, gestor):
        with as_user(gestor) as client:
            return client.post('/trips/planificar', data={
                'origen': '', 'destino': '', 'viajeros': 1,
            }).get_data(as_text=True)

    def test_el_campo_invalido_se_marca(self, as_user, gestor, seeded):
        html = self._formulario_en_blanco(as_user, gestor)

        assert 'is-invalid' in html

    def test_y_el_mensaje_va_donde_el_script_lo_busca(self, as_user, gestor, seeded):
        """Hermano del campo: el script mira en «campo.parentNode»."""
        import re

        html = self._formulario_en_blanco(as_user, gestor)
        bloques = re.findall(
            r'<input[^>]*is-invalid[^>]*>\s*(?:<small[^>]*>.*?</small>\s*)?'
            r'<div class="invalid-feedback"[^>]*>',
            html, re.S,
        )

        assert bloques, 'el mensaje ya no es hermano del campo inválido'

    def test_el_script_que_lo_retira_se_carga(self, as_user, gestor, seeded):
        html = self._formulario_en_blanco(as_user, gestor)

        assert 'js/main.js' in html
