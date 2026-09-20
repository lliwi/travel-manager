"""Scheduled maintenance: retention, expiry and cache cleanup.

Specification section 3.2 requires a retention and deletion policy for documents
and personal data; section 2.7 requires advisories to expire.
"""
import logging

from app.extensions import db
from app.tasks.celery_app import celery
from app.utils.timeutil import utcnow

logger = logging.getLogger(__name__)


@celery.task(name='app.tasks.maintenance.expire_advisories', bind=True)
def expire_advisories(self):
    """Mark advisories past their expiry as stale (section 2.7)."""
    from app.services import advisory_service

    expired = advisory_service.expire_stale()
    logger.info('Recomendaciones caducadas: %s', expired)
    return {'caducadas': expired}


@celery.task(name='app.tasks.maintenance.apply_retention', bind=True)
def apply_retention(self, dry_run=True):
    """Purge document originals whose retention period has elapsed.

    Defaults to a dry run. Erasing an original is irreversible and the retention
    window is an organisational decision, so the destructive pass is enabled
    deliberately rather than by default.
    """
    from app.models.document import Document
    from app.models.enums import AuditResourceType
    from app.services import audit_service, storage_service

    due = Document.query.filter(
        Document.retencion_hasta.isnot(None),
        Document.retencion_hasta < utcnow(),
        Document.objeto_storage.isnot(None),
    ).all()

    if dry_run:
        logger.info(
            'Retención (simulación): %s documentos superan su plazo de conservación.',
            len(due),
        )
        return {'modo': 'simulacion', 'candidatos': len(due)}

    purged = 0
    for document in due:
        try:
            storage_service.delete_document_object(document)
            document.objeto_storage = None
            # The metadata row, its hash and the audit trail survive: what is
            # erased is the content, not the evidence that it existed.
            audit_service.record(
                'document.purged_by_retention',
                recurso_tipo=AuditResourceType.DOCUMENTO,
                recurso_id=str(document.id),
                metadatos={
                    'trip_id': str(document.trip_id),
                    'hash': document.hash_sha256,
                    'retencion_hasta': document.retencion_hasta.isoformat(),
                },
                commit=False,
            )
            purged += 1
        except Exception:
            logger.exception('No se pudo purgar el documento %s', document.id)

    db.session.commit()
    logger.info('Retención aplicada: %s originales eliminados.', purged)
    return {'modo': 'real', 'purgados': purged}


@celery.task(name='app.tasks.maintenance.advance_trip_states', bind=True)
def advance_trip_states(self):
    """Move trips to «en curso» and «finalizado» as their dates pass."""
    from app.services import trip_service

    aplicados = trip_service.advance_states()
    if aplicados['en_curso'] or aplicados['finalizado']:
        logger.info(
            'Estados avanzados: %s en curso, %s finalizados.',
            aplicados['en_curso'], aplicados['finalizado'],
        )
    return aplicados


@celery.task(name='app.tasks.maintenance.purge_web_cache', bind=True)
def purge_web_cache(self, keep_days=90):
    """Drop expired web research cache entries."""
    from datetime import timedelta

    from app.models.advisory import WebFetch

    cutoff = utcnow() - timedelta(days=keep_days)
    deleted = WebFetch.query.filter(WebFetch.created_at < cutoff).delete()
    db.session.commit()

    logger.info('Caché de investigación: %s entradas eliminadas.', deleted)
    return {'eliminadas': deleted}


@celery.task(name='app.tasks.maintenance.verify_audit_chain', bind=True)
def verify_audit_chain(self, limit=5000):
    """Check the audit hash chain and shout if it is broken."""
    from app.services.audit_service import verify_chain

    ok, problems = verify_chain(limit=limit)
    if not ok:
        logger.error(
            'INTEGRIDAD DE AUDITORÍA COMPROMETIDA: %s anomalías detectadas.',
            len(problems),
        )
    return {'integra': ok, 'anomalias': len(problems)}
