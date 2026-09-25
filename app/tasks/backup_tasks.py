"""Backups and restores, too slow and too large for a web request."""
from app.tasks.celery_app import celery

#: A copy reads every document and a restore writes them all back. The
#: global 30 minutes is for work that should be quick; this is not.
LIMITE = 4 * 3600


@celery.task(name='app.tasks.backup.run_backup', bind=True,
             soft_time_limit=LIMITE, time_limit=LIMITE + 300,
             acks_late=False)
def run_backup(self, trabajo_id, frase_protegida):
    """Make a full backup. The passphrase arrives encrypted with the app key.

    ``acks_late`` off: redelivered after a lost worker, a half-done copy
    would start again from nothing under a job the screen already showed
    as running. Better to fail it and let a person launch another.
    """
    from app.services import backup_service

    trabajo = backup_service.ejecutar_copia(
        trabajo_id, backup_service._recuperar_frase(frase_protegida),
    )
    return {'estado': str(trabajo.estado)}


@celery.task(name='app.tasks.backup.run_restore', bind=True,
             soft_time_limit=LIMITE, time_limit=LIMITE + 300,
             acks_late=False)
def run_restore(self, trabajo_id, frase_protegida):
    """Restore an uploaded copy. Never retried: a restore runs once, on purpose."""
    from app.services import backup_service

    trabajo = backup_service.ejecutar_restauracion(
        trabajo_id, backup_service._recuperar_frase(frase_protegida),
    )
    return {'estado': str(trabajo.estado)}
