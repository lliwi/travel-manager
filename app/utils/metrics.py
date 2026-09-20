"""Prometheus exposition, and the part that has to survive four workers.

Gunicorn runs the application in several processes. A counter incremented in
one of them is invisible to the other three, so a scrape served by worker 2
would report a quarter of the traffic and nobody would notice the number was
wrong -- it would just look like a quiet day. ``prometheus_client`` solves this
with a shared directory the workers write into and a collector that sums across
them, which is what :func:`register` sets up when ``PROMETHEUS_MULTIPROC_DIR``
is present.

Without that variable the library runs in single-process mode, which is correct
for the development server and for the tests, and wrong in production. The
compose file sets it; :func:`register` says so in the logs when it is missing
and the process is not a development one, because a silently-quartered metric
is worse than no metric.

The business gauges are not counted here at all: they are queried from the
database when a scrape arrives. See :mod:`app.services.metrics_service` for
why.
"""

import os
import time

from flask import g, request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily

#: The specification's target is a p95 under 500 ms for ordinary queries, so
#: the buckets are placed to make that number readable straight off the
#: histogram rather than interpolated between two far-apart edges.
_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

PETICIONES = Counter(
    'travelmanager_http_requests_total',
    'Peticiones HTTP atendidas.',
    ['method', 'endpoint', 'status'],
)

LATENCIA = Histogram(
    'travelmanager_http_request_duration_seconds',
    'Duración de las peticiones HTTP.',
    ['method', 'endpoint'],
    buckets=_BUCKETS,
)


class _EstadoDelNegocio:
    """Gauges answered from the database each time a scrape arrives.

    Implemented as a collector rather than as gauges somebody has to remember
    to update: a gauge set from a request handler is stale the moment the
    handler returns, and a gauge set from a background job is a second thing
    that can stop working. Asking the database at scrape time cannot drift.
    """

    def __init__(self, app):
        self._app = app

    def collect(self):
        from app.services import metrics_service

        try:
            yield self._familia(
                'travelmanager_documents',
                'Documentos por estado del proceso.',
                'estado',
                metrics_service.documentos_por_estado(),
            )
            yield self._familia(
                'travelmanager_alerts_open',
                'Alertas abiertas por severidad.',
                'severidad',
                metrics_service.alertas_por_severidad(),
            )
            yield self._familia(
                'travelmanager_trips',
                'Viajes por estado.',
                'estado',
                metrics_service.viajes_por_estado(),
            )
            yield self._familia(
                'travelmanager_ai_runs',
                'Ejecuciones de IA por estado.',
                'estado',
                metrics_service.ejecuciones_ia_por_estado(),
            )
            yield self._familia(
                'travelmanager_ai_duration_ms_avg',
                'Duración media de cada tarea de IA, en milisegundos.',
                'tarea',
                metrics_service.latencia_ia_ms(),
            )
            yield self._familia(
                'travelmanager_queue_depth',
                'Tareas en cola pendientes de empezar.',
                'cola',
                metrics_service.profundidad_de_cola(self._app),
            )

            atascados = GaugeMetricFamily(
                'travelmanager_documents_stuck',
                'Documentos detenidos en un estado intermedio del proceso.',
            )
            atascados.add_metric([], metrics_service.documentos_atascados())
            yield atascados
        except Exception as exc:  # noqa: BLE001
            # A scrape must not 500. The HTTP metrics are collected from a
            # different registry path and are still worth serving when the
            # database is the thing that is broken -- which is exactly when
            # somebody is looking at this page.
            self._app.logger.warning('No se pudieron recoger las métricas: %s', exc)

    @staticmethod
    def _familia(nombre, ayuda, etiqueta, valores):
        familia = GaugeMetricFamily(nombre, ayuda, labels=[etiqueta])
        for clave, valor in sorted(valores.items()):
            familia.add_metric([str(clave)], valor)
        return familia


def _en_multiproceso():
    return bool(os.environ.get('PROMETHEUS_MULTIPROC_DIR'))


def registro_para_scrape(app):
    """Build the registry that answers one scrape.

    In multiprocess mode the HTTP counters are summed across the workers'
    shared files; the business gauges are added to the same registry so a
    scrape returns one document either way.
    """
    registro = CollectorRegistry()

    if _en_multiproceso():
        # The workers' shared files already hold every process's counters;
        # registering the in-process objects as well would count them twice.
        multiprocess.MultiProcessCollector(registro)
    else:
        for coleccionador in (PETICIONES, LATENCIA):
            registro.register(coleccionador)

    registro.register(_EstadoDelNegocio(app))
    return registro


def render(app):
    """Return the exposition body and its content type."""
    return generate_latest(registro_para_scrape(app)), CONTENT_TYPE_LATEST


def register(app):
    """Time every request and warn when workers would be measured separately."""
    directorio = os.environ.get('PROMETHEUS_MULTIPROC_DIR')
    if directorio:
        # The Celery containers inherit this variable from the shared
        # environment block and never serve a request, but the library writes
        # its files the first time a metric is touched and raises if the
        # directory is not there. Creating it costs nothing and removes a
        # crash that would only show up on the day a worker did touch one.
        os.makedirs(directorio, exist_ok=True)

    @app.before_request
    def _empezar_a_contar():
        g._metrics_started = time.perf_counter()

    @app.after_request
    def _anotar(response):
        inicio = g.pop('_metrics_started', None)
        if inicio is None:
            return response

        # The rule, not the path: labelling by path would create a new time
        # series per trip id and make the metric unusable within a week.
        endpoint = request.endpoint or 'desconocido'
        if endpoint == 'metrics':
            return response

        PETICIONES.labels(request.method, endpoint, response.status_code).inc()
        LATENCIA.labels(request.method, endpoint).observe(
            time.perf_counter() - inicio
        )
        return response

    if not _en_multiproceso() and not (app.config['DEBUG'] or app.config['TESTING']):
        app.logger.warning(
            'PROMETHEUS_MULTIPROC_DIR no está definida: cada worker contará sus '
            'propias peticiones y /metrics devolverá las de uno solo.'
        )
