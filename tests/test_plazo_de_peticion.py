"""One budget per web request, shared by everything that goes outside.

The bug this exists to prevent: the planning screen asked the flight connector
for 25 s, the lodging connector for another 25 and the model for 120, against a
Gunicorn worker timeout of 120. Nobody added them up. When Gunicorn won it sent
SIGABRT, which is not the same as failing: the route's error handling never
ran, the user got a bare 500 with no message, and the ``ai_runs`` row recording
what had been sent outside died with the transaction.
"""
import time

import pytest

from app.utils import http
from app.utils.errors import TransientError


@pytest.fixture(autouse=True)
def _techo_restaurado(app):
    """Put the ceiling back afterwards.

    The application object is built once for the whole session, so a test that
    leaves ``WEB_REQUEST_TIMEOUT`` at 1 does not fail here -- it fails in
    whatever runs later and makes an outbound call, with a message about a
    service that was never the problem.
    """
    original = app.config.get('WEB_REQUEST_TIMEOUT')
    yield
    app.config['WEB_REQUEST_TIMEOUT'] = original


@pytest.mark.unit
class TestElPresupuestoDeLaPeticion:
    def test_fuera_de_una_peticion_no_hay_plazo(self, app):
        """The Celery worker answers to its own limits, not to this one."""
        assert http.presupuesto_restante() is None

    def test_dentro_de_una_peticion_queda_el_techo_menos_lo_gastado(self, app):
        app.config['WEB_REQUEST_TIMEOUT'] = 100

        with app.test_request_context('/'):
            http.marcar_inicio_de_peticion()
            restante = http.presupuesto_restante()

        assert restante == pytest.approx(100 - http.MARGEN_DE_RESPUESTA, abs=1)

    def test_el_plazo_encoge_segun_pasa_el_tiempo(self, app):
        app.config['WEB_REQUEST_TIMEOUT'] = 100

        with app.test_request_context('/'):
            http.marcar_inicio_de_peticion()
            primero = http.presupuesto_restante()
            time.sleep(0.05)
            segundo = http.presupuesto_restante()

        assert segundo < primero

    def test_un_techo_desactivado_no_limita_nada(self, app):
        """Setting it to zero is how a deployment opts out deliberately."""
        app.config['WEB_REQUEST_TIMEOUT'] = 0

        with app.test_request_context('/'):
            http.marcar_inicio_de_peticion()
            assert http.presupuesto_restante() is None


@pytest.mark.unit
class TestNingunaLlamadaPuedePedirMasDeLoQueQueda:
    def test_se_recorta_al_plazo_restante(self, app):
        app.config['WEB_REQUEST_TIMEOUT'] = 30

        with app.test_request_context('/'):
            http.marcar_inicio_de_peticion()
            with http.cliente(timeout=120) as cliente:
                espera = cliente.timeout.connect

        # 120 era más de lo que la petición tiene entera.
        assert espera <= 30 - http.MARGEN_DE_RESPUESTA

    def test_un_timeout_que_cabe_se_respeta(self, app):
        app.config['WEB_REQUEST_TIMEOUT'] = 300

        with app.test_request_context('/'):
            http.marcar_inicio_de_peticion()
            with http.cliente(timeout=25) as cliente:
                assert cliente.timeout.connect == 25

    def test_fuera_de_una_peticion_se_respeta_lo_pedido(self, app):
        app.config['WEB_REQUEST_TIMEOUT'] = 30

        with http.cliente(timeout=600) as cliente:
            assert cliente.timeout.connect == 600

    def test_sin_plazo_no_se_abre_la_conexion(self, app):
        """Better an explained failure than a socket nobody will wait for."""
        app.config['WEB_REQUEST_TIMEOUT'] = 1

        with app.test_request_context('/'):
            http.marcar_inicio_de_peticion()
            # El margen de respuesta ya se come el techo entero.
            with pytest.raises(TransientError):
                http.cliente(timeout=10)

    def test_el_plazo_empieza_solo_en_cada_peticion(self, app, client):
        """A hook nobody registered is a budget that never applies."""
        respuesta = client.get('/healthz')

        assert respuesta.status_code == 200


@pytest.mark.unit
class TestElTechoSeDeclaraUnaVez:
    def test_gunicorn_y_la_aplicacion_leen_la_misma_variable(self):
        """Two numbers for one deadline is how a worker gets killed mid-call."""
        from pathlib import Path

        compose = (Path(__file__).resolve().parent.parent
                   / 'docker' / 'docker-compose.yml').read_text()

        assert '--timeout ${WEB_REQUEST_TIMEOUT:-180}' in compose
        assert 'WEB_REQUEST_TIMEOUT: ${WEB_REQUEST_TIMEOUT:-180}' in compose

    def test_el_valor_por_defecto_coincide(self, app):
        from app.config import Config

        assert Config.WEB_REQUEST_TIMEOUT == 180
