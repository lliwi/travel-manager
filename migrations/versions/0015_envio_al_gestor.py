"""un viaje que prepara quien viaja no se ve hasta que lo envía

Hasta ahora el gestor veía el borrador desde el primer momento y recibía el
aviso al crearse, cuando todavía no había nada que mirar. Ahora el viaje es
solo de quien lo prepara hasta que pulsa «Enviar».

Es una marca y no un estado más: «estado» dice en qué punto está el viaje y
esto dice a quién se le ha enseñado, y un estado nuevo habría que saltárselo
en cada viaje que abre un gestor.

Los viajes que ya existen quedan como enviados (falso): el gestor ya los veía,
y esconderle de golpe algo que tenía delante sería perderlo.

Revision ID: 0015_envio_al_gestor
Revises: 0014_tipo_documental_detectado
Create Date: 2026-11-15 10:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0015_envio_al_gestor'
down_revision = '0014_tipo_documental_detectado'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('trips', sa.Column(
        'pendiente_envio', sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column('trips', sa.Column(
        'enviado_en', sa.DateTime(timezone=True), nullable=True,
    ))


def downgrade():
    op.drop_column('trips', 'enviado_en')
    op.drop_column('trips', 'pendiente_envio')
