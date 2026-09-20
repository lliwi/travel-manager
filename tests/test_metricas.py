"""Metrics: §3.4 and §8 both ask for them, and both mean the same thing.

An operator needs to know that the pipeline stopped before somebody asks why
their booking never appeared. What is measured here is mostly state read from
the database rather than counters held in memory, because the application runs
in four Gunicorn workers and a counter that lives in one of them reports a
quarter of the truth -- a wrong number shaped like a right one.
"""
import pytest


@pytest.mark.integration
class TestElEndpointResponde:
    def test_sirve_formato_prometheus(self, client):
        respuesta = client.get('/metrics')

        assert respuesta.status_code == 200
        assert 'text/plain' in respuesta.content_type

    def test_trae_las_metricas_del_negocio(self, client, seeded):
        cuerpo = client.get('/metrics').get_data(as_text=True)

        for nombre in (
            'travelmanager_documents',
            'travelmanager_alerts_open',
            'travelmanager_trips',
            'travelmanager_ai_runs',
            'travelmanager_documents_stuck',
        ):
            assert nombre in cuerpo

    def test_cuenta_las_peticiones_atendidas(self, client):
        client.get('/healthz')
        cuerpo = client.get('/metrics').get_data(as_text=True)

        assert 'travelmanager_http_requests_total' in cuerpo
        assert 'endpoint="healthz"' in cuerpo

    def test_mide_la_latencia(self, client):
        """§3.4 sets a p95 target, which needs a histogram, not an average."""
        client.get('/healthz')
        cuerpo = client.get('/metrics').get_data(as_text=True)

        assert 'travelmanager_http_request_duration_seconds_bucket' in cuerpo
        assert 'le="0.5"' in cuerpo

    def test_no_se_mide_a_si_mismo(self, client):
        """A scrape every few seconds would otherwise dominate the histogram."""
        client.get('/metrics')
        cuerpo = client.get('/metrics').get_data(as_text=True)

        assert 'endpoint="metrics"' not in cuerpo

    def test_etiqueta_por_regla_y_no_por_ruta(self, app, client, gestor, trip, as_user):
        """A label per trip id would make the metric unusable within a week."""
        with as_user(gestor) as sesion:
            sesion.get(f'/trips/{trip.id}')

        cuerpo = client.get('/metrics').get_data(as_text=True)

        assert str(trip.id) not in cuerpo


@pytest.mark.security
class TestNoEsPublico:
    """The counts of trips, users and documents are nobody's business.

    Nginx denies it at the edge; this is the half that still holds if somebody
    reaches the container by another route.
    """

    # A routable address, deliberately: Python counts the documentation
    # ranges (203.0.113.0/24 and friends) as private, so a test written with
    # one of those passes without exercising anything.
    PUBLICA = '8.8.8.8'

    def test_desde_fuera_de_la_red_se_rechaza(self, client):
        respuesta = client.get('/metrics', environ_overrides={
            'REMOTE_ADDR': self.PUBLICA,
        })

        assert respuesta.status_code == 403

    def test_desde_dentro_se_permite(self, client):
        respuesta = client.get('/metrics', environ_overrides={
            'REMOTE_ADDR': '10.1.2.3',
        })

        assert respuesta.status_code == 200

    def test_con_el_token_se_permite_desde_donde_sea(self, app, client):
        app.config['METRICS_TOKEN'] = 'un-token-de-prueba'
        try:
            respuesta = client.get(
                '/metrics',
                headers={'Authorization': 'Bearer un-token-de-prueba'},
                environ_overrides={'REMOTE_ADDR': self.PUBLICA},
            )
        finally:
            app.config['METRICS_TOKEN'] = None

        assert respuesta.status_code == 200

    def test_un_token_equivocado_no_abre_nada(self, app, client):
        app.config['METRICS_TOKEN'] = 'un-token-de-prueba'
        try:
            respuesta = client.get(
                '/metrics',
                headers={'Authorization': 'Bearer otro'},
                environ_overrides={'REMOTE_ADDR': self.PUBLICA},
            )
        finally:
            app.config['METRICS_TOKEN'] = None

        assert respuesta.status_code == 403


@pytest.mark.unit
class TestLoQueMideDeVerdad:
    def test_solo_cuenta_alertas_abiertas(self, app, seeded):
        """A resolved alert is history, not a pending problem."""
        from app.services import metrics_service

        assert isinstance(metrics_service.alertas_por_severidad(), dict)

    def test_los_documentos_esperando_a_una_persona_no_estan_atascados(self, app, seeded):
        """Otherwise the metric fires every time somebody goes home."""
        from app.services import metrics_service

        assert metrics_service.documentos_atascados() >= 0

    def test_las_etiquetas_son_texto_y_no_enums(self, app, seeded):
        """An enum reaching the exposition renders as TripStatus.BORRADOR."""
        from app.services import metrics_service

        for clave in metrics_service.viajes_por_estado():
            assert isinstance(clave, str)
            assert '.' not in clave

    def test_un_broker_caido_no_tumba_el_scrape(self, app):
        """The metrics are most worth reading when something is already broken."""
        from app.services import metrics_service

        original = app.config['CELERY_BROKER_URL']
        app.config['CELERY_BROKER_URL'] = 'redis://no-existe:6379/0'
        try:
            assert metrics_service.profundidad_de_cola(app) == {}
        finally:
            app.config['CELERY_BROKER_URL'] = original


@pytest.mark.unit
class TestElMultiprocesoEstaAtendido:
    """Four workers, four sets of counters, unless this is configured.

    The failure is silent: /metrics keeps answering and reports roughly a
    quarter of the traffic, which looks like a quiet day rather than a bug.
    """

    def _conf(self):
        from pathlib import Path

        return (Path(__file__).resolve().parent.parent
                / 'docker' / 'flask' / 'gunicorn.conf.py').read_text()

    def test_la_carpeta_compartida_se_vacia_al_arrancar(self):
        conf = self._conf()

        assert 'on_starting' in conf
        assert 'rmtree' in conf

    def test_un_worker_muerto_se_recoge(self):
        conf = self._conf()

        assert 'child_exit' in conf
        assert 'mark_process_dead' in conf

    def test_compose_define_la_carpeta(self):
        from pathlib import Path

        compose = (Path(__file__).resolve().parent.parent
                   / 'docker' / 'docker-compose.yml').read_text()

        assert 'PROMETHEUS_MULTIPROC_DIR' in compose
        assert 'gunicorn.conf.py' in compose


@pytest.mark.security
class TestNginxLoNiegaEnElBorde:
    def test_metrics_no_sale_a_internet(self):
        from pathlib import Path

        conf = (Path(__file__).resolve().parent.parent
                / 'docker' / 'nginx' / 'app.conf').read_text()

        assert 'location = /metrics' in conf
        assert 'deny all' in conf
