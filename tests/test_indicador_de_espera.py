"""Something visible while the server takes its time.

The behaviour is in the browser, so what can be pinned here is the contract
the script relies on: that it is wired, that the slow forms say what they are
waiting for, and that a form which leaves the page is told apart -- a veil
over a page that never reloads would never be lifted.
"""
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1] / 'app'
SCRIPT = (RAIZ / 'static' / 'js' / 'main.js').read_text(encoding='utf-8')
ESTILOS = (RAIZ / 'static' / 'css' / 'main.css').read_text(encoding='utf-8')


class TestElGuion:
    def test_esta_conectado(self):
        arranque = SCRIPT[:SCRIPT.index('});')]

        assert 'showBusyWhileWaiting();' in arranque

    def test_respeta_un_envio_cancelado_y_no_tapa_lo_que_sale_de_la_pagina(self):
        assert 'event.defaultPrevented' in SCRIPT
        assert 'saleDeLaPagina(form, accion)' in SCRIPT
        assert "addEventListener('pageshow', ocultarEspera)" in SCRIPT

    def test_no_desactiva_el_boton(self):
        """A disabled button drops its name and value from the submission."""
        cuerpo = SCRIPT[SCRIPT.index('function showBusyWhileWaiting'):
                        SCRIPT.index('/** Refresh the page while')]

        assert '.disabled' not in cuerpo

    def test_tiene_sus_estilos(self):
        for clase in ('.velo-de-espera', '.velo-de-espera[hidden]', '.btn-esperando'):
            assert clase in ESTILOS


class TestLasPantallasLentas:
    @pytest.mark.parametrize('plantilla', [
        'trips/plan.html', 'trips/assistant.html', 'trips/summary.html',
        'trips/returns.html', 'macros/vuelos.html', 'advisories/index.html',
        'alerts/detail.html', 'documents/upload.html',
    ])
    def test_dicen_que_estan_esperando(self, plantilla):
        html = (RAIZ / 'templates' / plantilla).read_text(encoding='utf-8')

        assert 'data-loading="' in html

    def test_el_asistente_lo_dice_en_la_pagina(self, as_user, gestor):
        with as_user(gestor) as client:
            html = client.get('/trips/planificar').get_data(as_text=True)

        assert 'data-loading="Buscando opciones para el viaje…"' in html

    def test_comprar_fuera_abre_otra_pestana(self):
        """Which is how the script knows to leave that form alone."""
        html = (RAIZ / 'templates' / 'trips' / 'purchase.html').read_text(encoding='utf-8')

        assert 'target="_blank"' in html
