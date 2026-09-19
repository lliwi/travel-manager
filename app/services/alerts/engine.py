"""Alert engine: build the context, run the rules, reconcile the results."""
import logging
import time

from app.extensions import db
from app.models.alert import AlertRuleSetting, AlertRun
from app.models.catalog import Country
from app.models.enums import TRIP_CLOSED_STATES, AlertTrigger
from app.models.itinerary import (
    Accommodation,
    OtherService,
    TravelSegment,
    VehicleRental,
)
from app.services.alerts.base import TripContext, all_rules
from app.services.alerts.reconciler import reconcile

logger = logging.getLogger(__name__)


def build_context(trip):
    """Load everything the rules need, in one pass.

    Deliberately *not* scoped to any actor: the engine evaluates the whole trip
    on the manager's behalf. Per-actor filtering happens when alerts are read,
    not when they are computed -- otherwise a traveller's view of the itinerary
    would silently change which problems exist.
    """
    segments = TravelSegment.query.filter_by(
        trip_id=trip.id, is_deleted=False
    ).all()
    accommodations = Accommodation.query.filter_by(
        trip_id=trip.id, is_deleted=False
    ).all()
    vehicles = VehicleRental.query.filter_by(
        trip_id=trip.id, is_deleted=False
    ).all()
    services = OtherService.query.filter_by(
        trip_id=trip.id, is_deleted=False
    ).all()

    settings = {s.regla: s for s in AlertRuleSetting.query.all()}

    # Only the countries this trip actually touches.
    codes = {
        code for code in (
            [s.origen_pais for s in segments]
            + [s.destino_pais for s in segments]
            + [a.pais for a in accommodations]
            + [d.pais_codigo for d in trip.destinations]
        ) if code
    }
    countries = {}
    if codes:
        countries = {
            c.codigo: c
            for c in Country.query.filter(Country.codigo.in_(list(codes))).all()
        }

    travelers = trip.active_travelers
    traveler_documents = _load_traveler_documents(travelers)

    return TripContext(
        trip=trip,
        segments=segments,
        accommodations=accommodations,
        vehicles=vehicles,
        services=services,
        destinations=list(trip.destinations),
        travelers=travelers,
        settings=settings,
        countries=countries,
        traveler_documents=traveler_documents,
        documents=[d for d in trip.documents if not d.is_deleted],
    )


def _load_traveler_documents(travelers):
    """Passports and visas, only when the feature is enabled."""
    from app.services import settings_service

    if not settings_service.get_bool('DOCUMENTOS_VIAJERO_HABILITADOS', False):
        return {}

    from app.models.settings import TravelerDocument

    user_ids = [t.user_id for t in travelers]
    if not user_ids:
        return {}

    grouped = {}
    for document in TravelerDocument.query.filter(
        TravelerDocument.user_id.in_(user_ids),
        TravelerDocument.activo.is_(True),
    ).all():
        grouped.setdefault(document.user_id, []).append(document)
    return grouped


def evaluate(trip, rules=None):
    """Run every enabled rule over a trip.

    Returns:
        ``(candidatos, reglas_evaluadas)``.
    """
    ctx = build_context(trip)
    rules = rules if rules is not None else all_rules()

    candidates = []
    evaluated = 0

    for rule in rules:
        if not rule.is_enabled(ctx):
            continue
        evaluated += 1
        try:
            found = rule.evaluate(ctx) or []
        except Exception:
            # One broken rule must not stop the other seven from protecting the
            # traveller. The failure is logged loudly and the run continues.
            logger.exception(
                'La regla %s falló al evaluar el viaje %s', rule.codigo, trip.id
            )
            continue
        candidates.extend(found)

    return candidates, evaluated


def run(trip, trigger=AlertTrigger.MANUAL, actor=None, commit=True):
    """Evaluate a trip and reconcile the results with its existing alerts.

    Returns:
        The persisted :class:`AlertRun`.
    """
    start = time.monotonic()

    run_record = AlertRun(
        trip_id=trip.id,
        disparador=trigger,
        itinerary_version=trip.itinerary_version,
        actor_id=getattr(actor, 'id', None),
    )

    # A finished or cancelled trip is history; recomputing its alerts would
    # reopen problems nobody can act on any more.
    if trip.estado in TRIP_CLOSED_STATES:
        run_record.duracion_ms = int((time.monotonic() - start) * 1000)
        db.session.add(run_record)
        if commit:
            db.session.commit()
        return run_record

    try:
        candidates, evaluated = evaluate(trip)
        stats = reconcile(trip, candidates, actor=actor, commit=False)

        run_record.reglas_evaluadas = evaluated
        run_record.creadas = stats['creadas']
        run_record.actualizadas = stats['actualizadas']
        run_record.cerradas = stats['cerradas']
        run_record.sin_cambios = stats['sin_cambios']
    except Exception as exc:
        logger.exception('Fallo al recalcular las alertas del viaje %s', trip.id)
        run_record.error = str(exc)[:500]
        db.session.rollback()

    from app.utils.timeutil import utcnow

    trip.alertas_recalculadas_en = utcnow()
    run_record.duracion_ms = int((time.monotonic() - start) * 1000)

    db.session.add(run_record)
    if commit:
        db.session.commit()

    logger.info(
        'Alertas recalculadas para %s: +%s ~%s -%s (=%s) en %s ms',
        trip.referencia, run_record.creadas, run_record.actualizadas,
        run_record.cerradas, run_record.sin_cambios, run_record.duracion_ms,
    )
    return run_record
