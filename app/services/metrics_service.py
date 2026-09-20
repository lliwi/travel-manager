"""The numbers an operator needs to see before somebody complains.

Two kinds of measurement live here, and the distinction is the whole design.

**State is read from the database at scrape time.** How many documents are
stuck in each pipeline state, how many alerts are open, how many AI calls
failed -- these are queries, not counters. A query gives the same answer from
whichever of the four Gunicorn workers happens to serve the scrape, survives a
restart, and cannot drift between processes. It is also what makes the Celery
worker observable without running an HTTP server inside it: the worker's
outcomes land in these tables, so measuring the tables measures the worker.

**Flow is counted in the process**, because request latency exists nowhere
else. That part lives in :mod:`app.utils.metrics`, which has to deal with
aggregating across workers.

The one live signal that is neither is the broker queue depth: work that has
been accepted and not yet started is the difference between "slow" and
"stopped", and it exists only in Redis.
"""

from sqlalchemy import func

from app.extensions import db


def _por_estado(model, columna):
    """Count rows grouped by an enum column, skipping soft-deleted ones.

    Returns plain strings rather than enum members: a metric label is text, and
    letting an enum reach the exposition format would render it as
    ``DocumentProcessState.RECIBIDO``.
    """
    query = db.session.query(columna, func.count()).group_by(columna)

    if hasattr(model, 'is_deleted'):
        query = query.filter(model.is_deleted.is_(False))

    return {
        (getattr(valor, 'value', None) or str(valor)): total
        for valor, total in query.all()
        if valor is not None
    }


def documentos_por_estado():
    """Where documents are in the pipeline.

    The one to alert on is a non-terminal state whose count keeps growing:
    that is the pipeline having stopped, which otherwise shows up as somebody
    asking why their booking never appeared.
    """
    from app.models.document import Document

    return _por_estado(Document, Document.estado_proceso)


def alertas_por_severidad():
    """Open alerts only. A resolved alert is history, not a pending problem."""
    from app.models.alert import Alert
    from app.models.enums import AlertState

    query = (
        db.session.query(Alert.severidad, func.count())
        .filter(Alert.estado == AlertState.ABIERTA)
        .group_by(Alert.severidad)
    )

    return {
        (getattr(sev, 'value', None) or str(sev)): total
        for sev, total in query.all()
        if sev is not None
    }


def viajes_por_estado():
    from app.models.trip import Trip

    return _por_estado(Trip, Trip.estado)


def ejecuciones_ia_por_estado():
    """AI calls by outcome.

    A provider that has started refusing every call is invisible in the logs of
    a healthy-looking application: extraction simply stops producing data.
    """
    from app.models.ai import AIRun

    return _por_estado(AIRun, AIRun.estado)


def latencia_ia_ms():
    """Mean duration per task, over the runs that recorded one.

    A mean, not a percentile: percentiles over a table this size would cost a
    sort on every scrape, and what this is for is noticing that a model got
    four times slower after somebody changed it in the panel.
    """
    from app.models.ai import AIRun

    query = (
        db.session.query(AIRun.tarea, func.avg(AIRun.duracion_ms))
        .filter(AIRun.duracion_ms.isnot(None))
        .group_by(AIRun.tarea)
    )

    return {
        (getattr(tarea, 'value', None) or str(tarea)): float(media)
        for tarea, media in query.all()
        if tarea is not None and media is not None
    }


#: Every task is routed to Celery's default queue; there is no ``task_routes``
#: splitting the work, so this is the only list to measure.
COLA = 'celery'


def profundidad_de_cola(app):
    """Tasks accepted by the broker and not yet started.

    Celery keeps a Redis list per queue, so its length is the backlog, and a
    backlog that only grows is the difference between "slow" and "stopped".
    An unreachable broker returns nothing rather than raising: a scrape that
    failed entirely would take the working metrics down with it, and a broker
    that cannot be reached is what ``/readyz`` already reports.
    """
    try:
        import redis

        cliente = redis.Redis.from_url(
            app.config['CELERY_BROKER_URL'], socket_timeout=2
        )
        return {COLA: cliente.llen(COLA)}
    except Exception as exc:  # noqa: BLE001 - never break a scrape
        app.logger.debug('No se pudo medir la profundidad de cola: %s', exc)
        return {}


def documentos_atascados(minutos=30):
    """Documents sitting in a non-terminal state for too long.

    This is the metric worth waking somebody for. The counts by state say what
    the pipeline is holding; this says the pipeline has stopped moving, which
    is the failure that is otherwise only ever reported by a person.
    """
    from datetime import timedelta

    from app.models.document import Document
    from app.models.enums import DocumentProcessState
    from app.utils import timeutil

    terminales = {
        DocumentProcessState.APROBADO,
        DocumentProcessState.RECHAZADO,
        DocumentProcessState.DESCARTADO,
        DocumentProcessState.INFECTADO,
        DocumentProcessState.ERROR,
        # Waiting on a person is not being stuck: these two sit here until a
        # manager opens them, and counting them would make the metric fire
        # every time somebody goes home for the weekend.
        DocumentProcessState.PENDIENTE_REVISION,
        DocumentProcessState.REVISADO,
    }
    limite = timeutil.utcnow() - timedelta(minutes=minutos)

    return (
        db.session.query(func.count(Document.id))
        .filter(
            Document.is_deleted.is_(False),
            Document.estado_proceso.notin_(terminales),
            Document.updated_at < limite,
        )
        .scalar()
        or 0
    )
