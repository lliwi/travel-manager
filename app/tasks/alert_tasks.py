"""Alert recalculation tasks (specification section 2.4)."""
import logging

from app.extensions import db
from app.models.enums import TRIP_CLOSED_STATES, AlertTrigger
from app.models.trip import Trip
from app.tasks.celery_app import celery

logger = logging.getLogger(__name__)


@celery.task(
    name='app.tasks.alerts.recalculate_trip',
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    soft_time_limit=300,
    time_limit=360,
)
def recalculate_trip_alerts(self, trip_id, trigger=None, actor_id=None):
    """Re-run the alert engine over one trip."""
    import uuid

    from app.models.user import User
    from app.services.alerts import engine

    try:
        trip = db.session.get(Trip, uuid.UUID(str(trip_id)))
    except (ValueError, TypeError):
        trip = None

    if trip is None or trip.is_deleted:
        logger.info('Viaje %s inexistente; no se recalculan alertas.', trip_id)
        return {'estado': 'omitido', 'motivo': 'viaje_inexistente'}

    actor = None
    if actor_id:
        try:
            actor = db.session.get(User, uuid.UUID(str(actor_id)))
        except (ValueError, TypeError):
            actor = None

    run = engine.run(
        trip,
        trigger=AlertTrigger.coerce(trigger, AlertTrigger.ITINERARIO),
        actor=actor,
    )
    return {
        'estado': 'completado',
        'trip_id': str(trip.id),
        'creadas': run.creadas,
        'actualizadas': run.actualizadas,
        'cerradas': run.cerradas,
    }


@celery.task(
    name='app.tasks.alerts.recalculate_active_trips',
    bind=True,
    soft_time_limit=1500,
    time_limit=1800,
)
def recalculate_active_trips(self):
    """Nightly sweep over every trip that has not finished.

    Time passes even when nothing is edited: a passport creeps towards expiry
    and a trip moves from upcoming to in progress, so some alerts only become
    true with the calendar.
    """
    trips = Trip.query.filter(
        Trip.is_deleted.is_(False),
        Trip.estado.notin_([str(s) for s in TRIP_CLOSED_STATES]),
    ).all()

    processed = 0
    failed = 0
    for trip in trips:
        try:
            recalculate_trip_alerts.run(str(trip.id), str(AlertTrigger.PROGRAMADO))
            processed += 1
        except Exception:
            failed += 1
            logger.exception('Falló el recálculo programado del viaje %s', trip.id)

    logger.info(
        'Recálculo programado: %s viajes procesados, %s con error.', processed, failed
    )
    return {'estado': 'completado', 'procesados': processed, 'errores': failed}
