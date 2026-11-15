"""si el viaje se factura al cliente

Va junto al proyecto porque responde a la pregunta siguiente: no solo a qué
se imputa el viaje, sino si ese gasto se repercute. Los viajes que ya existen
quedan como no facturables: nadie dijo que lo fueran, y marcarlos por defecto
sería afirmar algo que no se sabe.

Revision ID: 0016_viaje_facturable
Revises: 0015_envio_al_gestor
Create Date: 2026-11-15 12:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0016_viaje_facturable'
down_revision = '0015_envio_al_gestor'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('trips', sa.Column(
        'facturable', sa.Boolean(), nullable=False, server_default=sa.false(),
    ))


def downgrade():
    op.drop_column('trips', 'facturable')
