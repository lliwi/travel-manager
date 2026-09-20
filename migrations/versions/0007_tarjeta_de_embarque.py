"""la tarjeta de embarque es un tipo documental propio

Distinta de un billete a propósito: el billete existe desde que se compra, la
tarjeta de embarque solo desde que alguien factura. Sin poder distinguirlas, un
vuelo con su reserva adjunta y sin facturar se ve exactamente igual que uno con
todo en regla, y no hay nada que avisar.

Se reescribe la restricción entera en lugar de ampliarla: un CHECK sobre una
lista no se amplía, se sustituye, y dejarla escrita completa es lo que permite
leer de un vistazo qué tipos existían cuando se aplicó esta migración.

Revision ID: 0007_tarjeta_de_embarque
Revises: 0006_segundo_factor
Create Date: 2026-09-20 21:00:00.000000

"""
from alembic import op

revision = '0007_tarjeta_de_embarque'
down_revision = '0006_segundo_factor'
branch_labels = None
depends_on = None

_ANTES = (
    'reserva', 'billete', 'factura', 'identidad', 'correspondencia', 'otro',
)
_DESPUES = (
    'reserva', 'billete', 'tarjeta_embarque', 'factura', 'identidad',
    'correspondencia', 'otro',
)


def _rehacer(valores):
    """Rewrite documents.tipo's CHECK with the given list.

    The name is given bare of the convention's prefix once: it is
    ``ck_documents_tipo_valido``, and writing the prefix twice is how a drop
    silently matches nothing.
    """
    lista = ', '.join(f"'{v}'" for v in valores)
    op.execute('ALTER TABLE documents DROP CONSTRAINT ck_documents_tipo_valido')
    op.execute(
        'ALTER TABLE documents ADD CONSTRAINT ck_documents_tipo_valido '
        f'CHECK (tipo IN ({lista}))'
    )


def upgrade():
    _rehacer(_DESPUES)


def downgrade():
    # Anything already filed as a boarding pass becomes a plain ticket, or the
    # constraint would refuse to be created and the downgrade would fail on a
    # database that had been in use.
    op.execute(
        "UPDATE documents SET tipo = 'billete' WHERE tipo = 'tarjeta_embarque'"
    )
    _rehacer(_ANTES)
