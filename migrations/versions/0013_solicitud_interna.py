"""la solicitud interna de viaje es un tipo documental propio

El formulario que rellena quien pide el viaje no es una reserva: al aprobarlo
completa el viaje -- motivo, proyecto, fechas, destino, quién viaja -- y no
añade nada al itinerario. Sin un tipo propio pasaba por la extracción de
reservas, que proponía un vuelo cada vez que la casilla «AVIÓN» estaba marcada.

Se reescribe la restricción entera, como en 0007_tarjeta_de_embarque.

Revision ID: 0013_solicitud_interna
Revises: 0012_copias_de_seguridad
Create Date: 2026-10-27 15:00:00.000000

"""
from alembic import op

revision = '0013_solicitud_interna'
down_revision = '0012_copias_de_seguridad'
branch_labels = None
depends_on = None

_ANTES = (
    'reserva', 'billete', 'tarjeta_embarque', 'factura', 'identidad',
    'correspondencia', 'otro',
)
_DESPUES = (
    'reserva', 'billete', 'tarjeta_embarque', 'factura', 'identidad',
    'correspondencia', 'solicitud_interna', 'otro',
)


def _rehacer(valores):
    lista = ', '.join(f"'{v}'" for v in valores)
    op.execute('ALTER TABLE documents DROP CONSTRAINT ck_documents_tipo_valido')
    op.execute(
        'ALTER TABLE documents ADD CONSTRAINT ck_documents_tipo_valido '
        f'CHECK (tipo IN ({lista}))'
    )


def upgrade():
    _rehacer(_DESPUES)


def downgrade():
    # A request filed before the downgrade stays as a document; it only loses
    # its own type, or the constraint would refuse to be created.
    op.execute(
        "UPDATE documents SET tipo = 'otro' WHERE tipo = 'solicitud_interna'"
    )
    _rehacer(_ANTES)
