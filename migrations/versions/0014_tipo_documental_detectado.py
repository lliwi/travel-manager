"""quién decidió el tipo de un documento

El tipo lo declaraba quien subía el documento, con «Reserva» propuesto por
defecto, y casi nadie lo cambiaba: una tarjeta de embarque o un talón de venta
quedaban archivados como reservas. Ahora se detecta por el contenido salvo que
la persona lo elija, y esta columna dice cuál de las dos cosas pasó. Los
documentos anteriores se quedan en nulo: no se sabe quién decidió su tipo, y
escribir «manual» afirmaría algo que nadie comprobó.

Revision ID: 0014_tipo_documental_detectado
Revises: 0013_solicitud_interna
Create Date: 2026-10-28 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0014_tipo_documental_detectado'
down_revision = '0013_solicitud_interna'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents') as batch_op:
        batch_op.add_column(sa.Column('tipo_origen', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('documents') as batch_op:
        batch_op.drop_column('tipo_origen')
