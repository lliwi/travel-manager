"""la planificación es una tarea de IA más

El asistente de planificación propone cómo hacer un viaje: qué medios tienen
sentido, cuánto se tarda normalmente y en qué zona conviene alojarse. Es una
tarea de IA como las otras, así que deja su fila en «ai_runs» como las demás, y
la restricción que enumera las tareas válidas tenía que enterarse.

Se reescribe entera en lugar de añadir un valor: una restricción CHECK sobre una
lista no se amplía, se sustituye, y dejarla escrita completa es lo que permite
leer de un vistazo qué tareas existían cuando se aplicó esta migración.

Revision ID: 0005_planificacion
Revises: 0004_notificaciones
Create Date: 2026-09-20 08:32:00.000000

"""
from alembic import op

revision = '0005_planificacion'
down_revision = '0004_notificaciones'
branch_labels = None
depends_on = None

_ANTES = (
    'extract_document', 'answer_trip_question', 'summarize_trip',
    'analyze_risks', 'research_public_info', 'classify_document',
    'explain_alert',
)
_DESPUES = _ANTES + ('plan_trip',)


def _rehacer(valores):
    """Rewrite both constraints with the given list of tasks.

    The names are given bare: the project's naming convention prefixes
    «ck_<tabla>_», so passing the full name once produced
    «ck_ai_runs_ck_ai_runs_tarea_valido» and dropped nothing.
    """
    lista = ', '.join(f"'{v}'" for v in valores)
    for tabla in ('ai_runs', 'ai_task_bindings'):
        op.execute(f'ALTER TABLE {tabla} DROP CONSTRAINT ck_{tabla}_tarea_valido')
        op.execute(
            f'ALTER TABLE {tabla} ADD CONSTRAINT ck_{tabla}_tarea_valido '
            f'CHECK (tarea IN ({lista}))'
        )


def upgrade():
    _rehacer(_DESPUES)


def downgrade():
    # Las filas de la tarea nueva no cabrían en la restricción anterior, y una
    # migración que deja la base inconsistente no es reversible: se borran.
    op.execute("DELETE FROM ai_task_bindings WHERE tarea = 'plan_trip'")
    op.execute("DELETE FROM ai_runs WHERE tarea = 'plan_trip'")
    _rehacer(_ANTES)
