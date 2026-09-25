"""AI work too slow for a web request: searching for better parameters."""
import logging

from app.tasks.celery_app import celery

logger = logging.getLogger(__name__)


#: One step is one trial, which is every case once or a few times through a
#: real model. The global 30 minutes is for requests that should be quick;
#: a trial with five repetitions on a large model legitimately is not.
UN_ENSAYO_COMO_MAXIMO = 2 * 3600


@celery.task(name='app.tasks.ai.run_autotune', bind=True,
             soft_time_limit=UN_ENSAYO_COMO_MAXIMO,
             time_limit=UN_ENSAYO_COMO_MAXIMO + 300)
def run_autotune(self, ajuste_id):
    """Run one step of a parameter search, and queue the next.

    No retry: a step that failed has already spent its model calls, and the
    row says why. A step lost with its worker is redelivered (``acks_late``)
    and carries on from the recorded trials.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    from app.services import autotune_service

    try:
        sigue = autotune_service.paso(ajuste_id, token=self.request.id)
    except SoftTimeLimitExceeded:
        autotune_service.marcar_error(
            ajuste_id,
            f'Un ensayo tardó más de {UN_ENSAYO_COMO_MAXIMO // 60} minutos. '
            f'Pruebe con menos repeticiones o un modelo más rápido.',
        )
        return {'estado': 'error'}

    if sigue:
        autotune_service.continuar(ajuste_id, self.request.id)
    return {'sigue': sigue}


@celery.task(name='app.tasks.ai.periodic_autotune', bind=True)
def periodic_autotune(self):
    """Queue a search for every measurable task, if enabled in Ajustes."""
    from app.services import autotune_service

    lanzados = autotune_service.autoajuste_periodico()
    logger.info('Autoajustes periódicos lanzados: %s', len(lanzados))
    return {'lanzados': len(lanzados)}
