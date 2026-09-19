"""Reconcile freshly computed candidates with the alerts already on record.

Four invariants, and the whole design exists to keep them:

1. **No duplicates.** A candidate whose ``dedup_key`` already has an open alert
   updates that alert instead of inserting a second one.
2. **Evidence stays current.** If the margin or the severity changed, the
   existing alert is refreshed rather than left stale.
3. **Resolved problems close themselves.** An alert whose condition no longer
   reproduces is closed automatically, marked ``cierre_automatico``.
4. **Human decisions are never undone.** An alert a manager accepted or
   dismissed is left exactly as they left it. Re-raising it on the next
   recalculation would make the accept button meaningless.
"""
import logging

from app.extensions import db
from app.models.alert import Alert, AlertTransition
from app.models.enums import (
    ALERT_ACKNOWLEDGED_STATES,
    AlertSeverity,
    AlertState,
)
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)


def reconcile(trip, candidates, actor=None, commit=True):
    """Apply a set of candidates to a trip's alerts.

    Returns:
        ``{'creadas': n, 'actualizadas': n, 'cerradas': n, 'sin_cambios': n}``.
    """
    existing = {a.dedup_key: a for a in Alert.query.filter_by(trip_id=trip.id).all()}
    by_key = {}
    for candidate in candidates:
        by_key[candidate.dedup_key(trip.id)] = candidate

    stats = {'creadas': 0, 'actualizadas': 0, 'cerradas': 0, 'sin_cambios': 0}
    now = utcnow()

    # --- Candidates that still hold ------------------------------------
    for key, candidate in by_key.items():
        alert = existing.get(key)

        if alert is None:
            db.session.add(_create(trip, key, candidate, now))
            stats['creadas'] += 1
            continue

        alert.vista_por_ultima_vez_en = now

        # Invariant 4: a decision already taken is respected.
        if alert.estado in ALERT_ACKNOWLEDGED_STATES:
            stats['sin_cambios'] += 1
            continue

        # An alert the engine closed earlier but which has reappeared is
        # genuinely a new occurrence of the same problem, so it reopens.
        if alert.estado is AlertState.RESUELTA and alert.cierre_automatico:
            alert.estado = AlertState.ABIERTA
            alert.resuelta_en = None
            alert.resuelta_por_id = None
            alert.cierre_automatico = False
            _transition(alert, AlertState.RESUELTA, AlertState.ABIERTA,
                        'La condición ha vuelto a producirse.', automatico=True)
            _apply(alert, candidate, now)
            stats['actualizadas'] += 1
            continue

        if alert.estado is AlertState.RESUELTA:
            # Closed by a person; leave it closed.
            stats['sin_cambios'] += 1
            continue

        if _changed(alert, candidate):
            _apply(alert, candidate, now)
            stats['actualizadas'] += 1
        else:
            stats['sin_cambios'] += 1

    # --- Alerts whose condition no longer reproduces --------------------
    for key, alert in existing.items():
        if key in by_key:
            continue
        if alert.estado is not AlertState.ABIERTA:
            continue

        alert.estado = AlertState.RESUELTA
        alert.resuelta_en = now
        alert.cierre_automatico = True
        _transition(
            alert, AlertState.ABIERTA, AlertState.RESUELTA,
            'La condición que originó la alerta ya no se cumple.', automatico=True,
        )
        stats['cerradas'] += 1

    if commit:
        db.session.commit()
    return stats


def _create(trip, key, candidate, now):
    """Build a new alert from a candidate."""
    alert = Alert(
        trip_id=trip.id,
        trip_traveler_id=candidate.trip_traveler_id,
        tipo=candidate.codigo,
        regla=candidate.regla,
        dedup_key=key,
        severidad=candidate.severidad,
        estado=AlertState.ABIERTA,
        titulo=candidate.titulo[:300],
        mensaje=candidate.mensaje,
        evidencia=candidate.evidencia,
        sugerencia=candidate.sugerencia,
        generada_en=now,
        vista_por_ultima_vez_en=now,
    )
    db.session.add(alert)
    db.session.flush()
    db.session.add(AlertTransition(
        alert_id=alert.id,
        estado_anterior=None,
        estado_nuevo=AlertState.ABIERTA,
        comentario='Alerta generada por el motor de validaciones.',
        automatico=True,
    ))
    return alert


def _changed(alert, candidate):
    """True when the candidate says something different from the stored alert."""
    return (
        alert.severidad is not candidate.severidad
        or alert.titulo != candidate.titulo[:300]
        or alert.mensaje != candidate.mensaje
        or (alert.evidencia or {}) != (candidate.evidencia or {})
    )


def _apply(alert, candidate, now):
    """Refresh a stored alert from a candidate."""
    previous_severity = alert.severidad

    alert.severidad = candidate.severidad
    alert.titulo = candidate.titulo[:300]
    alert.mensaje = candidate.mensaje
    alert.evidencia = candidate.evidencia
    alert.sugerencia = candidate.sugerencia
    alert.vista_por_ultima_vez_en = now

    # A severity change is worth recording: going from media to crítica is
    # information the manager needs, not a silent field update.
    if previous_severity is not candidate.severidad:
        db.session.add(AlertTransition(
            alert_id=alert.id,
            estado_anterior=alert.estado,
            estado_nuevo=alert.estado,
            comentario=(
                f'Severidad actualizada de «{_label(previous_severity)}» a '
                f'«{_label(candidate.severidad)}».'
            ),
            automatico=True,
        ))
    return alert


def _transition(alert, anterior, nuevo, comentario, actor=None, automatico=False):
    """Record a state change of an alert."""
    db.session.add(AlertTransition(
        alert_id=alert.id,
        estado_anterior=anterior,
        estado_nuevo=nuevo,
        comentario=comentario,
        actor_id=getattr(actor, 'id', None),
        automatico=automatico,
    ))


def _label(severity):
    return severity.label if isinstance(severity, AlertSeverity) else str(severity)
