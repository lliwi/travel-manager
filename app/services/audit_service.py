"""Audit trail writing and verification.

Specification section 2.1 lists the operations that must be audited: user
creation and deletion, role changes, document access and download, modifications
and AI queries. Section 3.2 requires the trail to be immutable or
integrity-controlled, which the hash chain in ``AuditEvent`` provides.

Every write goes through :func:`record`. Nothing else constructs an AuditEvent.
"""
import contextlib
import logging

from flask import g, has_request_context, request
from sqlalchemy import select

from app.extensions import db
from app.models.audit import GENESIS_HASH, AuditEvent
from app.models.enums import AuditResult

logger = logging.getLogger(__name__)

#: Metadata keys that must never be written to the trail, whatever a caller
#: passes. Section 3.2: logs without unnecessary PII or secrets.
FORBIDDEN_METADATA_KEYS = frozenset({
    'password', 'password_hash', 'api_key', 'token', 'secret',
    'contenido', 'texto', 'payload', 'prompt', 'respuesta',
})

#: Longest string kept in a metadata value; longer ones are truncated.
MAX_METADATA_VALUE = 500


def record(
    accion,
    recurso_tipo=None,
    recurso_id=None,
    actor=None,
    resultado=AuditResult.EXITO,
    metadatos=None,
    commit=True,
):
    """Append one event to the audit trail.

    Args:
        accion: Dotted action name, e.g. ``'document.downloaded'``.
        recurso_tipo: An :class:`AuditResourceType` member.
        recurso_id: Identifier of the affected resource, stringified.
        actor: The acting user, or None for system actions. Defaults to
            ``current_user`` when a request is in flight.
        resultado: Whether the action succeeded, was denied or errored.
        metadatos: Minimised extra context. Sensitive keys are dropped.
        commit: Commit immediately. Pass False to join the caller's transaction.

    Returns:
        The persisted :class:`AuditEvent`, or None if writing failed.
    """
    try:
        actor = _resolve_actor(actor)
        event = AuditEvent(
            actor_id=getattr(actor, 'id', None),
            actor_username=getattr(actor, 'username', None),
            accion=accion,
            recurso_tipo=recurso_tipo,
            recurso_id=str(recurso_id) if recurso_id is not None else None,
            resultado=resultado,
            metadatos=_sanitise(metadatos),
        )
        _attach_request_context(event)
        _chain(event)

        db.session.add(event)
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return event
    except Exception:
        # An audit failure must never take down the operation being audited, but
        # it must be loud: a silently missing trail is worse than a noisy one.
        logger.exception('No se pudo registrar el evento de auditoría: %s', accion)
        if commit:
            db.session.rollback()
        return None


def record_denied(accion, recurso_tipo=None, recurso_id=None, actor=None, motivo=None):
    """Record a refused access attempt.

    Called by ``authorization_service`` so every denial leaves a trace, which is
    what makes an IDOR probe visible after the fact.
    """
    return record(
        accion,
        recurso_tipo=recurso_tipo,
        recurso_id=recurso_id,
        actor=actor,
        resultado=AuditResult.DENEGADO,
        metadatos={'motivo': motivo} if motivo else None,
    )


def _resolve_actor(actor):
    """Fall back to the logged-in user when no actor was supplied."""
    if actor is not None:
        return actor
    if not has_request_context():
        return None
    # Best-effort: outside a login context there is simply no actor, and an
    # audit entry with no actor is correct for a system action.
    with contextlib.suppress(Exception):
        from flask_login import current_user

        if current_user and current_user.is_authenticated:
            return current_user._get_current_object()
    return None


def _attach_request_context(event):
    """Copy IP, user agent and correlation id off the current request."""
    if not has_request_context():
        return
    event.ip_address = request.remote_addr
    user_agent = request.headers.get('User-Agent')
    event.user_agent = user_agent[:400] if user_agent else None
    event.request_method = request.method
    event.request_path = request.path[:500]
    event.correlation_id = getattr(g, 'correlation_id', None)


def _sanitise(metadatos):
    """Drop forbidden keys and truncate long values."""
    if not metadatos:
        return None
    clean = {}
    for key, value in metadatos.items():
        lowered = str(key).lower()
        if any(bad in lowered for bad in FORBIDDEN_METADATA_KEYS):
            continue
        if isinstance(value, str) and len(value) > MAX_METADATA_VALUE:
            value = value[:MAX_METADATA_VALUE] + '…'
        elif isinstance(value, (list, tuple)):
            value = [str(v)[:120] for v in value[:20]]
        elif not isinstance(value, (str, int, float, bool, dict, type(None))):
            value = str(value)[:MAX_METADATA_VALUE]
        clean[key] = value
    return clean or None


def _chain(event):
    """Link the event to the previous one and seal it with its own hash."""
    from app.utils.timeutil import utcnow

    if event.created_at is None:
        event.created_at = utcnow()

    previous = db.session.execute(
        select(AuditEvent.hash_propio).order_by(AuditEvent.id.desc()).limit(1)
    ).scalar()
    event.hash_anterior = previous or GENESIS_HASH
    event.hash_propio = event.compute_hash()


def verify_chain(limit=None, start_id=None):
    """Walk the trail and report any break in the hash chain.

    Returns:
        ``(ok, problemas)`` where ``problemas`` lists dicts describing each
        broken row. Used by the administration integrity check.
    """
    query = AuditEvent.query.order_by(AuditEvent.id.asc())
    if start_id is not None:
        query = query.filter(AuditEvent.id >= start_id)
    if limit:
        query = query.limit(limit)

    problems = []
    expected_previous = None
    for event in query:
        if expected_previous is None:
            expected_previous = event.hash_anterior
        if event.hash_anterior != expected_previous:
            problems.append({
                'id': event.id,
                'motivo': 'enlace_roto',
                'esperado': expected_previous,
                'encontrado': event.hash_anterior,
            })
        if event.hash_propio != event.compute_hash():
            problems.append({
                'id': event.id,
                'motivo': 'contenido_alterado',
                'accion': event.accion,
            })
        expected_previous = event.hash_propio

    return (not problems), problems
