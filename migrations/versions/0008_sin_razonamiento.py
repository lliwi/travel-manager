"""poder apagar el razonamiento de un modelo que razona

Un modelo de razonamiento gasta tokens de salida pensando antes de escribir, y
con un presupuesto ajustado se queda sin sitio para la respuesta: la llamada
termina «bien», sin contenido, y lo único que se veía era «el modelo no
devolvió un JSON interpretable». Pasó con el planificador, cuyo prompt creció
al llevar vuelos y alojamientos reales.

Columna propia y no una clave dentro de «parametros»: es una decisión que toma
un administrador a sabiendas, como el máximo de tokens o la temperatura, no una
manía del endpoint. Lo que sí vive en «parametros» es lo contrario —que este
endpoint ya rechazó el parámetro una vez—, porque eso no lo decide nadie, se
descubre.

Revision ID: 0008_sin_razonamiento
Revises: 0007_tarjeta_de_embarque
Create Date: 2026-09-24 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0008_sin_razonamiento'
down_revision = '0007_tarjeta_de_embarque'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'ai_provider_configs',
        sa.Column('sin_razonamiento', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade():
    op.drop_column('ai_provider_configs', 'sin_razonamiento')
