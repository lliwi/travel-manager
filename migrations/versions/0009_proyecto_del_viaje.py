"""a qué proyecto se imputa un viaje

Opcional a propósito: no todo viaje pertenece a uno, y hacerlo obligatorio
obligaría a inventar un valor para los que no, que es como una columna deja de
significar nada.

Texto libre y no una tabla de proyectos: aquí no se administran proyectos, se
anotan. Una tabla traería altas, bajas, nombres que cambian y una pantalla que
mantener, para lo que hoy es una etiqueta con la que agrupar un informe. Lleva
índice porque agrupar por ella es justamente para lo que existe.

Revision ID: 0009_proyecto_del_viaje
Revises: 0008_sin_razonamiento
Create Date: 2026-09-24 11:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0009_proyecto_del_viaje'
down_revision = '0008_sin_razonamiento'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('trips', sa.Column('proyecto', sa.String(length=120), nullable=True))
    op.create_index('ix_trips_proyecto', 'trips', ['proyecto'])


def downgrade():
    op.drop_index('ix_trips_proyecto', table_name='trips')
    op.drop_column('trips', 'proyecto')
