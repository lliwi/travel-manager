"""Celery application and the Flask-aware task base.

Specification section 3.1 asks for an asynchronous job queue for OCR, antivirus,
AI extraction, research and alert recalculation.

Every task runs inside a Flask application context, provided by
:class:`FlaskTask` rather than by each task building its own. That means a task
body reads like ordinary application code and cannot forget the context.
"""
import logging
import os

from celery import Celery, Task, signals
from celery.schedules import crontab

logger = logging.getLogger(__name__)


class FlaskTask(Task):
    """Task base that pushes an application context around every run."""

    _flask_app = None

    @property
    def flask_app(self):
        """The Flask app, created once per worker process."""
        if FlaskTask._flask_app is None:
            from app import create_app

            FlaskTask._flask_app = create_app(os.getenv('FLASK_ENV', 'production'))
        return FlaskTask._flask_app

    def __call__(self, *args, **kwargs):
        with self.flask_app.app_context():
            return self.run(*args, **kwargs)

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Roll back so a failed task never leaves a half-written transaction."""
        try:
            with self.flask_app.app_context():
                from app.extensions import db

                db.session.rollback()
        except Exception:
            logger.exception('No se pudo revertir la sesión tras el fallo de %s', task_id)
        super().on_failure(exc, task_id, args, kwargs, einfo)


def make_celery():
    """Build the Celery application."""
    celery = Celery(
        'travel_manager',
        broker=os.getenv('CELERY_BROKER_URL', 'redis://redis:6379/1'),
        backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://redis:6379/2'),
        include=[
            'app.tasks.document_tasks',
            'app.tasks.alert_tasks',
            'app.tasks.maintenance_tasks',
        ],
        task_cls=FlaskTask,
    )

    celery.conf.update(
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        # Timestamps are handled in UTC throughout; the interface localises.
        timezone='UTC',
        enable_utc=True,
        task_track_started=True,
        task_time_limit=1800,
        task_soft_time_limit=1500,
        # OCR and inference are heavy and slow: one task at a time per worker
        # child keeps memory bounded and makes progress observable.
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=100,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        result_expires=86400,
        beat_schedule={
            # Every quarter hour, not nightly: a trip that started at 07:55
            # should not still read «confirmado» at lunchtime.
            'avanzar-estados-de-viaje': {
                'task': 'app.tasks.maintenance.advance_trip_states',
                'schedule': crontab(minute='*/15'),
            },
            'recalcular-alertas-viajes-activos': {
                'task': 'app.tasks.alerts.recalculate_active_trips',
                'schedule': crontab(hour=3, minute=0),
            },
            # Una vez al día basta: un país no cambia su nivel de aviso cada
            # hora, y releer sus fichas cuesta una descarga por viaje vivo.
            'vigilar-fuentes-de-recomendaciones': {
                'task': 'app.tasks.maintenance.watch_advisory_sources',
                'schedule': crontab(hour=6, minute=0),
            },
            'caducar-recomendaciones': {
                'task': 'app.tasks.maintenance.expire_advisories',
                'schedule': crontab(hour=3, minute=30),
            },
            'aplicar-retencion': {
                'task': 'app.tasks.maintenance.apply_retention',
                'schedule': crontab(hour=4, minute=0),
            },
            'limpiar-cache-web': {
                'task': 'app.tasks.maintenance.purge_web_cache',
                'schedule': crontab(hour=4, minute=30),
            },
            # A verifiable audit trail that nobody verifies is a trail nobody
            # trusts. Tampering only counts as detected if something looks.
            'verificar-cadena-de-auditoria': {
                'task': 'app.tasks.maintenance.verify_audit_chain',
                'schedule': crontab(hour=5, minute=0),
            },
        },
    )
    return celery


celery = make_celery()


@signals.setup_logging.connect
def _configure_worker_logging(**_kwargs):
    """Let the application configure logging instead of Celery.

    Celery installs its own handlers at the level it was started with, which
    at DEBUG makes botocore print the signature of every S3 request and buries
    the application's own messages. Connecting to ``setup_logging`` tells
    Celery to keep its hands off; ``create_app`` has already set up handlers,
    filters and the noisy-library levels.
    """
    from app import create_app

    create_app(os.getenv('FLASK_ENV', 'production'))
